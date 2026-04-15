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
import queue
import time
import threading
from collections import deque

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

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


# Callback for wake word activation
_wake_word_callback = None


def set_wake_word_callback(callback):
    """Set callback for when wake word is detected."""
    global _wake_word_callback
    _wake_word_callback = callback


class AudioCaptureService:
    def __init__(self):
        log.info(f"Loading Whisper model: {config.WHISPER_MODEL}")
        self._model = WhisperModel(
            config.WHISPER_MODEL,
            device="cpu",
            compute_type="int8",
        )
        self._audio_queue: queue.Queue = queue.Queue()
        self._running = False
        self._loop = None  # Set by set_loop() before run()

    def _transcribe(self, audio: np.ndarray) -> str:
        """
        Transcribe audio with VAD filter (Silero VAD built into faster-whisper).
        Returns empty string if no speech detected.
        """
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

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            callback=self._record_callback,
            blocksize=1024,
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
                            ts = time.time()
                            db.insert_transcript(ts, text)
                            log.info(f"Heard: {text[:100]}")
                            # Emit to UI so dashboard shows what was heard
                            try:
                                from ui.bridge import get_bridge
                                get_bridge().transcript_heard.emit(text[:120])
                            except Exception:
                                pass

                            # Check for wake word
                            # run_coroutine_threadsafe is the correct way to
                            # schedule an async callback from a thread executor.
                            if _check_wake_word(text) and _wake_word_callback and self._loop:
                                asyncio.run_coroutine_threadsafe(
                                    _wake_word_callback(text), self._loop
                                )
                    except Exception as e:
                        log.error(f"Transcription error: {e}")

                except queue.Empty:
                    continue
                except Exception as e:
                    log.error(f"Audio record loop error: {e}")

    def set_loop(self, loop) -> None:
        """Must be called from main before run() so threads can schedule callbacks."""
        self._loop = loop

    async def run(self) -> None:
        self._running = True
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        await self._loop.run_in_executor(None, self._record_loop)

    def stop(self) -> None:
        self._running = False
