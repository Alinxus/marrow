"""
Audio capture + transcription + wake word detection.

Continuously records mic in configurable chunks (default 5s).
Transcribes with faster-whisper (local, no API cost).
Uses VAD filter built into faster-whisper (Silero-based).
Wake word detection: listens for "Marrow" or "Hey Marrow" to activate on-demand mode.

Design notes from Omi:
  - Omi had a separate VAD pass before transcription to skip silent chunks.
    faster-whisper's vad_filter=True does this internally (Silero VAD),
    so we get the same quality without an extra model.
  - Rolling buffer approach: accumulate blocks from sounddevice callback
    until we have CHUNK_SECONDS of audio, then process.
  - Run blocking record loop in a thread executor so asyncio stays unblocked.
"""

import asyncio
import logging
import platform
import queue
import time
import threading
from collections import deque

import numpy as np
import sounddevice as sd

import config
from storage import db

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHUNK_SECONDS = config.AUDIO_CHUNK_SECONDS

# Wake word detection
WAKE_WORDS = ["marrow", "hey marrow"]

# Adaptive silence: track rolling RMS over last N chunks to calibrate threshold
_rms_history: deque = deque(maxlen=20)
_BASE_SILENCE_THRESHOLD = config.SILENCE_THRESHOLD


def _compute_rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))


def _adaptive_threshold() -> float:
    """
    Dynamic silence threshold based on recent audio environment.
    If environment is noisy, threshold rises so we don't over-transcribe.
    Falls back to configured base if no history yet.
    """
    if len(_rms_history) < 5:
        return _BASE_SILENCE_THRESHOLD
    median = float(np.median(list(_rms_history)))
    # Threshold = 40% of recent median RMS, floored at base
    return max(_BASE_SILENCE_THRESHOLD, median * 0.4)


def _is_silent(audio: np.ndarray) -> bool:
    rms = _compute_rms(audio)
    _rms_history.append(rms)
    return rms < _adaptive_threshold()


def _check_wake_word(text: str) -> bool:
    """Check if transcribed text contains wake word."""
    if not text:
        return False

    text_lower = text.lower().strip()

    for wake_word in WAKE_WORDS:
        if wake_word in text_lower:
            log.info(f"Wake word detected: {text[:50]}")
            return True

    return False


def _select_input_device():
    """Resolve input device from config or first valid input device."""
    configured = (config.AUDIO_INPUT_DEVICE or "").strip()
    if configured:
        try:
            if configured.isdigit():
                return int(configured)
            return configured
        except Exception:
            pass

    try:
        devices = sd.query_devices()
        for idx, dev in enumerate(devices):
            if int(dev.get("max_input_channels", 0)) > 0:
                return idx
    except Exception:
        return None
    return None


# Callback for wake word activation
_wake_word_callback = None
_mac_mic_perm_warned = False


def set_wake_word_callback(callback):
    """Set callback for when wake word is detected."""
    global _wake_word_callback
    _wake_word_callback = callback


class AudioCaptureService:
    def __init__(self):
        self._audio_backend_error = ""
        self._unavailable_notified = False
        if not config.AUDIO_ENABLED:
            self._deepgram_key = ""
            self._model = None
            self._audio_queue: queue.Queue = queue.Queue()
            self._running = False
            self._loop = None
            log.info("Audio service initialized in disabled mode")
            return

        self._deepgram_key = config.DEEPGRAM_API_KEY
        backend = (getattr(config, "AUDIO_STT_BACKEND", "auto") or "auto").lower()

        if backend == "none":
            log.info("Audio STT backend set to 'none' — listening disabled")
            self._deepgram_key = ""
            self._model = None
            self._audio_queue: queue.Queue = queue.Queue()
            self._running = False
            self._loop = None
            return

        if backend == "deepgram" and not self._deepgram_key:
            log.warning(
                "AUDIO_STT_BACKEND=deepgram but DEEPGRAM_API_KEY missing — listening disabled"
            )
            self._model = None
            self._audio_queue: queue.Queue = queue.Queue()
            self._running = False
            self._loop = None
            return

        # Prefer Deepgram when explicitly requested or when auto+key exists.
        use_deepgram = bool(self._deepgram_key) and backend in ("auto", "deepgram")
        use_whisper = backend == "whisper" or (backend == "auto" and not use_deepgram)

        # On macOS, default auto mode avoids local whisper import crashes on some CPUs.
        if platform.system() == "Darwin" and backend == "auto" and not use_deepgram:
            use_whisper = False
            log.warning(
                "macOS auto audio mode: local Whisper disabled by default for stability. "
                "Set AUDIO_STT_BACKEND=whisper to force local Whisper, or configure Deepgram."
            )

        if use_deepgram:
            log.info("Audio: Deepgram streaming enabled")
            self._model = None
        elif use_whisper:
            log.info(f"Audio: Whisper fallback ({config.WHISPER_MODEL})")
            try:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(
                    config.WHISPER_MODEL,
                    device="cpu",
                    compute_type="int8",
                )
            except Exception as e:
                # On some macOS/CPU combos, wheels can crash with illegal instructions.
                # Degrade gracefully instead of crashing the whole app.
                log.error(f"Whisper init failed; disabling audio capture: {e}")
                self._model = None
                self._deepgram_key = ""
                self._running = False
                self._audio_backend_error = str(e)
        else:
            log.info(
                "Audio STT backend unavailable in current mode — listening disabled"
            )
            self._model = None
            self._deepgram_key = ""

        self._audio_queue: queue.Queue = queue.Queue()
        self._running = False
        self._loop = None  # Set by set_loop() before run()

    def _transcribe(self, audio: np.ndarray) -> str:
        """
        Transcribe audio with VAD filter (Silero VAD built into faster-whisper).
        Returns empty string if no speech detected.
        """
        if self._model is None:
            return ""

        segments, info = self._model.transcribe(
            audio,
            language="en",
            vad_filter=True,
            vad_parameters={
                "threshold": 0.4,
                "min_speech_duration_ms": 200,
                "min_silence_duration_ms": 500,
            },
            beam_size=5,
            best_of=5,
            temperature=0.0,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        # Only skip if language confidence is extremely low (hallucinated noise)
        if text and info.language_probability < 0.3:
            log.debug(
                f"Very low language confidence ({info.language_probability:.2f}), skipping"
            )
            return ""
        return text

    def _record_callback(self, indata, frames, time_info, status):
        if status:
            log.warning(f"Audio status: {status}")
        self._audio_queue.put(indata.copy())

    def _record_loop(self) -> None:
        """
        Blocking loop: accumulate audio chunks, transcribe when buffer is full.
        Runs in a thread executor (called from async run()).
        """
        buffer = []
        samples_per_chunk = SAMPLE_RATE * CHUNK_SECONDS
        # Check that an input device exists before opening the stream
        try:
            devices = sd.query_devices()
            input_devices = [d for d in devices if d["max_input_channels"] > 0]
            if not input_devices:
                log.warning("No audio input device found — audio capture disabled")
                try:
                    from ui.bridge import get_bridge

                    get_bridge().mic_active.emit(False)
                except Exception:
                    pass
                return
        except Exception as e:
            log.warning(f"Audio device query failed: {e} — audio capture disabled")
            try:
                from ui.bridge import get_bridge

                get_bridge().mic_active.emit(False)
            except Exception:
                pass
            return

        log.info(
            f"Audio capture loop started ({CHUNK_SECONDS}s chunks, {SAMPLE_RATE}Hz)"
        )

        # Signal UI that mic is active
        try:
            from ui.bridge import get_bridge

            get_bridge().mic_active.emit(True)
        except Exception:
            pass

        selected_device = _select_input_device()
        log.info(
            f"Audio input device: {selected_device if selected_device is not None else 'default'}"
        )
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            callback=self._record_callback,
            blocksize=1024,
            device=selected_device,
        ):
            while self._running:
                try:
                    chunk = self._audio_queue.get(timeout=1.0)
                    buffer.append(chunk)

                    total_samples = sum(c.shape[0] for c in buffer)
                    if total_samples < samples_per_chunk:
                        continue

                    # Full chunk accumulated — process it
                    audio = np.concatenate(buffer, axis=0).flatten()
                    buffer = []

                    if _is_silent(audio):
                        log.debug("Audio chunk silent — skipping transcription")
                        continue

                    try:
                        text = self._transcribe(audio)
                        if text:
                            self._on_transcript(text)
                    except Exception as e:
                        log.error(f"Transcription error: {e}")

                except queue.Empty:
                    continue
                except Exception as e:
                    log.error(f"Audio record loop error: {e}")

    def _on_transcript(self, text: str) -> None:
        """Called from any backend when a transcript is ready."""
        if not text.strip():
            return
        ts = time.time()
        from storage import db as _db

        _db.insert_transcript(ts, text)
        log.info(f"Heard: {text[:100]}")

        try:
            from ui.bridge import get_bridge

            get_bridge().transcript_heard.emit(text[:120])
        except Exception:
            pass

        if _check_wake_word(text) and _wake_word_callback and self._loop:
            asyncio.run_coroutine_threadsafe(_wake_word_callback(text), self._loop)

    async def _run_deepgram(self) -> None:
        """
        Real-time streaming via Deepgram SDK.
        Sends raw mic audio as 16kHz mono PCM, receives transcripts live.
        """
        try:
            from deepgram import (
                DeepgramClient,
                LiveTranscriptionEvents,
                LiveOptions,
                Microphone,
            )
        except ImportError:
            log.warning("deepgram-sdk not installed — falling back to Whisper")
            await self._loop.run_in_executor(None, self._record_loop)
            return

        try:
            from ui.bridge import get_bridge

            get_bridge().mic_active.emit(True)
        except Exception:
            pass

        log.info("Deepgram streaming started")
        dg = DeepgramClient(self._deepgram_key)

        while self._running:
            try:
                conn = dg.listen.live.v("1")

                def on_message(self_dg, result, **kwargs):
                    try:
                        sentence = result.channel.alternatives[0].transcript
                        if sentence and result.is_final:
                            self._on_transcript(sentence)
                    except Exception:
                        pass

                def on_error(self_dg, error, **kwargs):
                    log.warning(f"Deepgram error: {error}")

                conn.on(LiveTranscriptionEvents.Transcript, on_message)
                conn.on(LiveTranscriptionEvents.Error, on_error)

                options = LiveOptions(
                    model="nova-2",
                    language="en",
                    smart_format=True,
                    vad_events=True,
                    endpointing=300,
                    interim_results=False,
                )

                if not conn.start(options):
                    log.error("Deepgram connection failed — falling back to Whisper")
                    await self._loop.run_in_executor(None, self._record_loop)
                    return

                mic = Microphone(conn.send)
                mic.start()

                while self._running:
                    await asyncio.sleep(1)

                mic.finish()
                conn.finish()

            except Exception as e:
                log.error(f"Deepgram stream error: {e} — reconnecting in 5s")
                await asyncio.sleep(5)

    def set_loop(self, loop) -> None:
        """Must be called from main before run() so threads can schedule callbacks."""
        self._loop = loop

    async def run(self) -> None:
        global _mac_mic_perm_warned
        if not config.AUDIO_ENABLED:
            log.info("Audio service disabled (AUDIO_ENABLED=0)")
            return

        self._running = True

        if self._model is None and not self._deepgram_key:
            if not self._unavailable_notified:
                self._unavailable_notified = True
                log.warning("Audio backend unavailable; running in screen-only mode.")
                try:
                    from ui.bridge import get_bridge

                    get_bridge().mic_active.emit(False)
                    msg = "Audio backend unsupported on this machine. Running screen-only mode."
                    if self._audio_backend_error:
                        msg += " (faster-whisper unavailable)"
                    get_bridge().toast_requested.emit("Marrow", msg, 2)
                except Exception:
                    pass
            # Keep task alive so supervisor doesn't restart-spam.
            while self._running:
                await asyncio.sleep(60)
            return

        if self._loop is None:
            self._loop = asyncio.get_running_loop()

        try:
            if self._deepgram_key:
                await self._run_deepgram()
            else:
                await self._loop.run_in_executor(None, self._record_loop)
        except Exception as e:
            msg = str(e)
            if "Error querying device -1" in msg or "Invalid device" in msg:
                log.error(
                    "No valid microphone input device detected. Audio capture paused."
                )
                while self._running:
                    await asyncio.sleep(60)
                return
            if platform.system() == "Darwin" and (
                "permission" in msg.lower() or "not authorized" in msg.lower()
            ):
                if not _mac_mic_perm_warned:
                    _mac_mic_perm_warned = True
                    log.warning(
                        "macOS microphone permission likely missing. Enable Microphone for Terminal/Python in System Settings > Privacy & Security > Microphone, then restart Marrow."
                    )
                    try:
                        from ui.bridge import get_bridge

                        get_bridge().toast_requested.emit(
                            "Marrow",
                            "Enable Microphone permission for Terminal/Python (System Settings > Privacy & Security).",
                            2,
                        )
                    except Exception:
                        pass
                while self._running:
                    await asyncio.sleep(60)
                return
            raise

    def stop(self) -> None:
        self._running = False
