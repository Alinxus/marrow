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
import os
import platform
import queue
import re
import time
import threading
from collections import deque

# Reduce OpenBLAS/OMP memory usage before NumPy import.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

import numpy as np
try:
    import sounddevice as sd
except Exception as exc:
    sd = None
    _SOUNDDEVICE_IMPORT_ERROR = str(exc)
else:
    _SOUNDDEVICE_IMPORT_ERROR = ""

import config
from storage import db

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHUNK_SECONDS = max(
    2, int(getattr(config, "AUDIO_ACTIVE_CHUNK_SECONDS", config.AUDIO_CHUNK_SECONDS))
)

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


def _float_audio_to_pcm16_bytes(audio: np.ndarray) -> bytes:
    clipped = np.clip(audio.astype(np.float32), -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16).tobytes()


def _check_wake_word(text: str) -> bool:
    """Check if transcribed text contains wake word."""
    if not text:
        return False

    text_lower = text.lower().strip()

    for wake_word in WAKE_WORDS:
        # Word-boundary match: avoids false triggers on "borrow", "sorrow", etc.
        pattern = r"\b" + re.escape(wake_word) + r"\b"
        if re.search(pattern, text_lower):
            log.info(f"Wake word detected: {text[:50]}")
            return True

    return False


def _select_input_device():
    """Resolve input device from config or first valid input device."""
    if sd is None:
        return None
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
_conversation_turn_callback = None


def _emit_audio_status(message: str) -> None:
    try:
        from ui.bridge import get_bridge

        get_bridge().audio_debug.emit(message[:220])
    except Exception:
        pass


def set_wake_word_callback(callback):
    """Set callback for when wake word is detected."""
    global _wake_word_callback
    _wake_word_callback = callback


def set_conversation_turn_callback(callback):
    """Set callback for conversation turn handling while conversation mode is active."""
    global _conversation_turn_callback
    _conversation_turn_callback = callback


class AudioCaptureService:
    def __init__(self):
        self._audio_backend_error = ""
        self._unavailable_notified = False
        self._last_transcript_text = ""
        self._last_transcript_ts = 0.0
        self._speech_gate_until = 0.0
        if not config.AUDIO_ENABLED:
            self._deepgram_key = ""
            self._model = None
            self._audio_queue: queue.Queue = queue.Queue()
            self._running = False
            self._loop = None
            log.info("Audio service initialized in disabled mode")
            _emit_audio_status("audio disabled")
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
            _emit_audio_status("audio backend set to none")
            return

        if backend == "deepgram" and not self._deepgram_key:
            log.warning(
                "AUDIO_STT_BACKEND=deepgram but DEEPGRAM_API_KEY missing — listening disabled"
            )
            self._model = None
            self._audio_queue: queue.Queue = queue.Queue()
            self._running = False
            self._loop = None
            _emit_audio_status("deepgram selected but API key missing")
            return

        # Prefer Deepgram when explicitly requested or when auto+key exists.
        use_deepgram = bool(self._deepgram_key) and backend in ("auto", "deepgram")
        use_whisper = backend == "whisper" or (backend == "auto" and not use_deepgram)

        if use_deepgram:
            log.info("Audio: Deepgram streaming enabled")
            self._model = None
            _emit_audio_status("deepgram ready")
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
                _emit_audio_status("whisper init failed")
        else:
            log.info(
                "Audio STT backend unavailable in current mode — listening disabled"
            )
            self._model = None
            self._deepgram_key = ""
            _emit_audio_status("no audio backend available")

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
        if sd is None:
            log.warning(
                "sounddevice unavailable - audio capture disabled: %s",
                _SOUNDDEVICE_IMPORT_ERROR or "import failed",
            )
            _emit_audio_status("audio input unavailable")
            return
        buffer = []
        samples_per_chunk = SAMPLE_RATE * CHUNK_SECONDS
        # Check that an input device exists before opening the stream
        try:
            devices = sd.query_devices()
            input_devices = [d for d in devices if d["max_input_channels"] > 0]
            if not input_devices:
                log.warning("No audio input device found — audio capture disabled")
                try:
                    from storage import db as _db

                    _db.upsert_runtime_component(
                        "audio_capture", "paused", "no audio input device"
                    )
                except Exception:
                    pass
                try:
                    from ui.bridge import get_bridge

                    get_bridge().mic_active.emit(False)
                except Exception:
                    pass
                return
        except Exception as e:
            log.warning(f"Audio device query failed: {e} — audio capture disabled")
            try:
                from storage import db as _db

                _db.upsert_runtime_component(
                    "audio_capture", "error", f"device query failed: {str(e)[:140]}"
                )
            except Exception:
                pass
            try:
                from ui.bridge import get_bridge

                get_bridge().mic_active.emit(False)
            except Exception:
                pass
            return

        log.info(
            f"Audio capture loop started ({CHUNK_SECONDS}s chunks, {SAMPLE_RATE}Hz)"
        )
        try:
            from storage import db as _db

            _db.upsert_runtime_component(
                "audio_capture", "active", f"chunk={CHUNK_SECONDS}s sr={SAMPLE_RATE}"
            )
        except Exception:
            pass
        _emit_audio_status(f"listening in {CHUNK_SECONDS}s chunks")

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
        _emit_audio_status(
            f"mic active on device {selected_device if selected_device is not None else 'default'}"
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
                        _emit_audio_status("listening: silence")
                        continue

                    try:
                        _emit_audio_status("transcribing")
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
        if len(text.strip()) < max(
            1, int(getattr(config, "AUDIO_MIN_TRANSCRIPT_CHARS", 3))
        ):
            return
        ts = time.time()
        normalized = " ".join(text.lower().split())
        if (
            normalized
            and normalized == self._last_transcript_text
            and (ts - float(self._last_transcript_ts)) < 2.5
        ):
            return
        self._last_transcript_text = normalized
        self._last_transcript_ts = ts
        from storage import db as _db

        _db.insert_transcript(ts, text)
        log.info(f"Heard: {text[:100]}")
        _emit_audio_status(f"heard: {text[:80]}")

        try:
            from ui.bridge import get_bridge

            get_bridge().transcript_heard.emit(text[:120])
        except Exception:
            pass

        if _check_wake_word(text) and _wake_word_callback and self._loop:
            _emit_audio_status("wake word detected")
            asyncio.run_coroutine_threadsafe(_wake_word_callback(text), self._loop)
            return

        # If conversation mode is active, route utterance directly without wake word.
        try:
            from brain import conversation
            from voice.speak import cancel_speaking

            if (
                config.CONVERSATION_ENABLED
                and conversation.is_active()
                and _conversation_turn_callback
                and self._loop
            ):
                # Any utterance in active conversation extends session timeout.
                conversation.touch_session()
                # User is speaking while Marrow may be speaking: barge-in support.
                cancel_speaking()
                _emit_audio_status("conversation turn")
                asyncio.run_coroutine_threadsafe(
                    _conversation_turn_callback(text), self._loop
                )
                return
        except Exception:
            pass

        # If the user is speaking directly to Marrow without a clean wake-word hit,
        # treat short imperative utterances as activation attempts to reduce dead air.
        lowered = text.lower().strip()
        direct_prefixes = (
            "marrow ",
            "hey marrow ",
            "can you ",
            "could you ",
            "please ",
        )
        if (
            config.CONVERSATION_ENABLED
            and any(lowered.startswith(prefix) for prefix in direct_prefixes)
            and self._loop
        ):
            if _conversation_turn_callback:
                try:
                    from brain import conversation

                    conversation.activate_session()
                except Exception:
                    pass
                _emit_audio_status("direct request detected")
                asyncio.run_coroutine_threadsafe(
                    _conversation_turn_callback(text), self._loop
                )

    async def _run_deepgram(self) -> None:
        """
        Real-time streaming via Deepgram SDK.
        Sends raw mic audio as 16kHz mono PCM, receives transcripts live.
        """
        if sd is None:
            log.warning(
                "sounddevice unavailable - Deepgram live capture disabled: %s",
                _SOUNDDEVICE_IMPORT_ERROR or "import failed",
            )
            _emit_audio_status("audio input unavailable")
            return
        try:
            from deepgram import (
                DeepgramClient,
                LiveTranscriptionEvents,
                LiveOptions,
            )
        except ImportError:
            log.warning("deepgram-sdk not installed - falling back to Whisper")
            await self._loop.run_in_executor(None, self._record_loop)
            return

        try:
            from ui.bridge import get_bridge

            get_bridge().mic_active.emit(True)
        except Exception:
            pass

        log.info("Deepgram streaming started")
        _emit_audio_status("deepgram streaming started")
        dg = DeepgramClient(self._deepgram_key)
        reconnect_delay = max(
            0.5, float(getattr(config, "DEEPGRAM_RECONNECT_BASE_SECONDS", 1.0))
        )
        selected_device = _select_input_device()
        gate_enabled = bool(getattr(config, "DEEPGRAM_VAD_GATE_ENABLED", True))
        hangover_seconds = max(
            0.15, int(getattr(config, "DEEPGRAM_VAD_HANGOVER_MS", 650)) / 1000.0
        )

        while self._running:
            audio_q: queue.Queue = queue.Queue(maxsize=256)
            stream = None
            conn = None
            partial_text = {"value": ""}
            try:
                conn = dg.listen.live.v("1")
                last_partial_emit = {"text": "", "ts": 0.0}

                def on_message(self_dg, result, **kwargs):
                    try:
                        sentence = result.channel.alternatives[0].transcript
                        if not sentence:
                            return
                        partial_text["value"] = sentence
                        now = time.time()
                        normalized = " ".join(sentence.lower().split())
                        if (
                            normalized
                            and normalized != last_partial_emit["text"]
                            and (now - float(last_partial_emit["ts"])) > 0.35
                        ):
                            last_partial_emit["text"] = normalized
                            last_partial_emit["ts"] = now
                            _emit_audio_status(f"hearing: {sentence[:80]}")
                        if result.is_final or getattr(result, "speech_final", False):
                            self._on_transcript(sentence)
                            partial_text["value"] = ""
                    except Exception:
                        pass

                def on_error(self_dg, error, **kwargs):
                    log.warning(f"Deepgram error: {error}")
                    _emit_audio_status(f"deepgram error: {error}")

                conn.on(LiveTranscriptionEvents.Transcript, on_message)
                conn.on(LiveTranscriptionEvents.Error, on_error)

                options = LiveOptions(
                    model=getattr(config, "DEEPGRAM_MODEL", "nova-3"),
                    language=getattr(config, "DEEPGRAM_LANGUAGE", "en"),
                    smart_format=True,
                    vad_events=True,
                    endpointing=int(getattr(config, "DEEPGRAM_ENDPOINTING_MS", 180)),
                    utterance_end_ms=str(
                        int(getattr(config, "DEEPGRAM_UTTERANCE_END_MS", 700))
                    ),
                    interim_results=True,
                )

                if not conn.start(options):
                    log.error("Deepgram connection failed - retrying")
                    _emit_audio_status("deepgram connect failed")
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 1.8, 8.0)
                    continue

                def stream_callback(indata, frames, time_info, status):
                    if status:
                        log.warning(f"Deepgram audio status: {status}")
                    try:
                        audio_q.put_nowait(indata.copy())
                    except queue.Full:
                        try:
                            audio_q.get_nowait()
                        except Exception:
                            pass
                        try:
                            audio_q.put_nowait(indata.copy())
                        except Exception:
                            pass

                stream = sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    channels=1,
                    dtype="float32",
                    callback=stream_callback,
                    blocksize=512,
                    device=selected_device,
                )
                stream.start()
                reconnect_delay = max(
                    0.5, float(getattr(config, "DEEPGRAM_RECONNECT_BASE_SECONDS", 1.0))
                )
                _emit_audio_status("deepgram live with speech gate")

                while self._running:
                    try:
                        chunk = await asyncio.to_thread(audio_q.get, True, 1.0)
                    except queue.Empty:
                        continue

                    audio = np.asarray(chunk, dtype=np.float32).flatten()
                    if audio.size == 0:
                        continue

                    now = time.time()
                    if gate_enabled:
                        if not _is_silent(audio):
                            self._speech_gate_until = now + hangover_seconds
                            _emit_audio_status("speech detected")
                        elif now > self._speech_gate_until:
                            continue

                    conn.send(_float_audio_to_pcm16_bytes(audio))

            except Exception as e:
                if partial_text.get("value"):
                    try:
                        self._on_transcript(partial_text["value"])
                    except Exception:
                        pass
                    partial_text["value"] = ""
                log.error(
                    f"Deepgram stream error: {e} - reconnecting in {reconnect_delay:.1f}s"
                )
                _emit_audio_status("deepgram reconnecting")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 1.8, 8.0)
            finally:
                try:
                    if stream is not None:
                        stream.stop()
                        stream.close()
                except Exception:
                    pass
                try:
                    if conn is not None:
                        conn.finish()
                except Exception:
                    pass

    def set_loop(self, loop) -> None:
        """Must be called from main before run() so threads can schedule callbacks."""
        self._loop = loop

    async def run(self) -> None:
        global _mac_mic_perm_warned
        if not config.AUDIO_ENABLED:
            log.info("Audio service disabled (AUDIO_ENABLED=0)")
            return

        self._running = True
        if sd is None:
            self._audio_backend_error = _SOUNDDEVICE_IMPORT_ERROR or "sounddevice unavailable"
        try:
            from storage import db as _db

            _db.upsert_runtime_component(
                "audio_capture", "starting", "audio run entered"
            )
        except Exception:
            pass

        if self._model is None and not self._deepgram_key:
            if not self._unavailable_notified:
                self._unavailable_notified = True
                log.warning("Audio backend unavailable; running in screen-only mode.")
                _emit_audio_status("audio backend unavailable")
                try:
                    from ui.bridge import get_bridge

                    get_bridge().mic_active.emit(False)
                    msg = "Audio backend unsupported on this machine. Running screen-only mode."
                    if self._audio_backend_error:
                        msg += " (faster-whisper unavailable)"
                    get_bridge().toast_requested.emit("Marrow", msg, 2)
                except Exception:
                    pass
            try:
                from storage import db as _db

                _db.upsert_runtime_component(
                    "audio_capture", "unavailable", "audio backend unavailable"
                )
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
                try:
                    from storage import db as _db

                    _db.upsert_runtime_component(
                        "audio_capture", "paused", "invalid microphone input device"
                    )
                except Exception:
                    pass
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
