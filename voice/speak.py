"""
Voice output.

Primary: ElevenLabs with true PCM streaming + sounddevice.
  - Uses PCM output format (no MP3 decode step)
  - Streams chunks to sounddevice as they arrive (low latency)
  - Filler phrases ("One moment.") via fast SAPI before async fetch starts
  - Cancellation: cancel_speaking() stops mid-stream
  - Singleton client — not recreated per call

Fallback: Windows SAPI via PowerShell (zero dependencies, always works).

Omi reference:
  - ElevenLabs turbo v2.5, voice Sloane (BAMYoBHLZM7lJgJAmFz0)
  - Chunk buffering with filler phrases for immediate audio feedback
  - Streaming audio so first words play ~300ms after request
"""

import asyncio
import logging
import queue
import random
import threading
from typing import Optional

import numpy as np
import sounddevice as sd

import config

# ─── Kokoro state ──────────────────────────────────────────────────────────────
_kokoro_pipeline = None
_kokoro_lock = threading.Lock()

log = logging.getLogger(__name__)

# ─── State ─────────────────────────────────────────────────────────────────────

_cancel_event = threading.Event()
_speaking_lock = asyncio.Lock()

# Singleton sync ElevenLabs client (lazy init)
_el_client = None

FILLER_PHRASES = [
    "One moment.",
    "Let me check.",
    "Sure.",
    "On it.",
    "Right.",
]

ELEVENLABS_VOICE_SETTINGS = {
    "stability": 0.45,
    "similarity_boost": 0.85,
    "style": 0.1,
    "use_speaker_boost": True,
}


# ─── Public API ────────────────────────────────────────────────────────────────

async def speak(text: str) -> None:
    """
    Speak text. Priority chain:
      1. ElevenLabs — best quality, needs API key (~$5/mo starter)
      2. Kokoro     — free, local, Apache 2.0, 82M params, near-ElevenLabs quality
      3. Windows SAPI — always-available fallback
    """
    async with _speaking_lock:
        _cancel_event.clear()
        if config.ELEVENLABS_API_KEY:
            try:
                await _speak_elevenlabs(text)
                return
            except Exception as e:
                log.warning(f"ElevenLabs failed: {e}")
        if _kokoro_available():
            try:
                await _speak_kokoro(text)
                return
            except Exception as e:
                log.warning(f"Kokoro failed: {e}")
        await _speak_system(text)


async def speak_filler() -> None:
    """
    Speak an immediate short filler via SAPI (no API latency).
    Use before a slow async operation so the user hears something right away.
    """
    phrase = random.choice(FILLER_PHRASES)
    await _speak_system(phrase)


def cancel_speaking() -> None:
    """Signal the current speech to stop. Safe to call from any thread."""
    _cancel_event.set()


# ─── ElevenLabs streaming ──────────────────────────────────────────────────────

def _get_el_client():
    global _el_client
    if _el_client is None:
        from elevenlabs.client import ElevenLabs
        _el_client = ElevenLabs(api_key=config.ELEVENLABS_API_KEY)
    return _el_client


def _elevenlabs_stream_thread(text: str, chunk_q: queue.Queue) -> None:
    """
    Runs in a thread: fetches PCM chunks from ElevenLabs, puts them in queue.
    Sentinel None signals end of stream.
    """
    try:
        client = _get_el_client()
        audio_iter = client.text_to_speech.convert_as_stream(
            text=text,
            voice_id=config.MARROW_VOICE_ID,
            model_id="eleven_turbo_v2_5",
            output_format="pcm_16000",  # raw PCM, no decode needed
            voice_settings=ELEVENLABS_VOICE_SETTINGS,
        )
        for chunk in audio_iter:
            if _cancel_event.is_set():
                break
            if chunk:
                chunk_q.put(chunk)
    except Exception as e:
        log.error(f"ElevenLabs stream thread error: {e}")
        chunk_q.put(e)  # signal error to consumer
    finally:
        chunk_q.put(None)  # sentinel


def _play_pcm_from_queue(chunk_q: queue.Queue, sample_rate: int = 16000) -> None:
    """
    Runs in executor: plays PCM chunks from queue via sounddevice RawOutputStream.
    Starts playing immediately when first chunk arrives (true streaming).
    """
    try:
        with sd.RawOutputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
            blocksize=4096,
        ) as stream:
            while True:
                if _cancel_event.is_set():
                    break
                try:
                    item = chunk_q.get(timeout=5.0)
                except queue.Empty:
                    break

                if item is None:
                    break  # end of stream sentinel
                if isinstance(item, Exception):
                    raise item
                stream.write(item)
    except Exception as e:
        if not _cancel_event.is_set():
            log.error(f"PCM playback error: {e}")


async def _speak_elevenlabs(text: str) -> None:
    """
    True streaming TTS:
      1. Spawn fetch thread → puts PCM chunks in queue
      2. Run playback in executor → plays chunks as they arrive
      Both run concurrently; first audio plays ~300ms after call.
    """
    chunk_q: queue.Queue = queue.Queue(maxsize=64)

    # Fetch thread starts immediately
    fetch_thread = threading.Thread(
        target=_elevenlabs_stream_thread,
        args=(text, chunk_q),
        daemon=True,
    )
    fetch_thread.start()

    # Playback runs in thread executor (blocks sounddevice, not asyncio)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _play_pcm_from_queue, chunk_q)

    fetch_thread.join(timeout=2.0)


# ─── Kokoro local TTS ─────────────────────────────────────────────────────────

def _kokoro_available() -> bool:
    try:
        import kokoro_onnx  # noqa: F401
        return True
    except ImportError:
        return False


def _get_kokoro_pipeline():
    """
    Lazy-init Kokoro ONNX pipeline (singleton). Thread-safe.
    Downloads ~310MB model on first call to ~/.cache/kokoro-onnx.
    """
    global _kokoro_pipeline
    with _kokoro_lock:
        if _kokoro_pipeline is None:
            from kokoro_onnx import Kokoro
            # Downloads model + voices automatically on first call
            _kokoro_pipeline = Kokoro("kokoro-v1.0.onnx", "voices-v1.0.bin")
        return _kokoro_pipeline


def _kokoro_generate_thread(text: str, chunk_q: queue.Queue) -> None:
    """
    Runs in a thread: generates audio samples from Kokoro ONNX, puts numpy arrays in queue.
    Sentinel None signals end of stream.
    """
    try:
        kokoro = _get_kokoro_pipeline()
        # af_heart = American female, warm voice
        samples, sample_rate = kokoro.create(
            text,
            voice="af_heart",
            speed=1.0,
            lang="en-us",
        )
        if _cancel_event.is_set():
            return
        if samples is not None and len(samples) > 0:
            # Kokoro ONNX outputs float32 — convert to int16
            audio_int16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
            # Send in 8192-sample chunks so playback starts immediately
            chunk_size = 8192
            for i in range(0, len(audio_int16), chunk_size):
                if _cancel_event.is_set():
                    break
                chunk_q.put(audio_int16[i:i + chunk_size])
            chunk_q.put((None, sample_rate))  # sentinel with rate
    except Exception as e:
        log.error(f"Kokoro generate thread error: {e}")
        chunk_q.put(e)
    finally:
        chunk_q.put(None)


def _play_numpy_from_queue(chunk_q: queue.Queue) -> None:
    """
    Play int16 numpy arrays from queue via sounddevice.
    Waits for first chunk to determine sample rate, then opens stream.
    """
    stream = None
    try:
        # Drain the queue: first item determines stream params
        while True:
            if _cancel_event.is_set():
                return
            try:
                item = chunk_q.get(timeout=15.0)
            except queue.Empty:
                return

            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            # Sentinel tuple (None, sample_rate) ends the stream gracefully
            if isinstance(item, tuple) and item[0] is None:
                return

            # item is a numpy int16 array
            if stream is None:
                # Open stream with kokoro-onnx default rate (24kHz)
                stream = sd.RawOutputStream(
                    samplerate=24000,
                    channels=1,
                    dtype="int16",
                    blocksize=4096,
                )
                stream.start()

            stream.write(item.tobytes())
    except Exception as e:
        if not _cancel_event.is_set():
            log.error(f"Kokoro playback error: {e}")
    finally:
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass


async def _speak_kokoro(text: str) -> None:
    """
    Local TTS via Kokoro ONNX (Apache 2.0, 24kHz, near-ElevenLabs quality, free).
    First call downloads ~310MB model; subsequent calls are fast.
    """
    chunk_q: queue.Queue = queue.Queue(maxsize=64)

    gen_thread = threading.Thread(
        target=_kokoro_generate_thread,
        args=(text, chunk_q),
        daemon=True,
    )
    gen_thread.start()

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _play_numpy_from_queue, chunk_q)

    gen_thread.join(timeout=5.0)


# ─── Windows SAPI fallback ────────────────────────────────────────────────────

async def _speak_system(text: str) -> None:
    """Windows SAPI via PowerShell. Instant, no API, offline."""
    # Escape for PowerShell string
    safe = text.replace("'", "''").replace('"', '`"')
    cmd = (
        "PowerShell -NoProfile -Command \""
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Rate = 1; "
        f"$s.Speak('{safe}')\""
    )
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
