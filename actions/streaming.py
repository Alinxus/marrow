"""
Streaming responses — generate text and speak it as it arrives.

The approach:
  - Stream LLM response token by token
  - Buffer until a sentence boundary (. ! ? or ~80 chars)
  - Speak each sentence chunk immediately via speak()
  - User hears the first sentence ~300ms after generation starts

This is meaningfully different from batch TTS:
  - Batch: wait for ALL text, then speak → 2-5s delay
  - Streaming: speak first sentence while rest is still generating → <1s
"""

import asyncio
import logging
import re
from typing import Callable, Optional

import config
from brain.llm import get_client
from voice.speak import speak, cancel_speaking

log = logging.getLogger(__name__)

# Sentence boundary pattern — split on . ! ? followed by space or end
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|(?<=[.!?])$")
_MIN_CHUNK_CHARS = 60  # Don't speak tiny fragments


class StreamingExecutor:
    """
    Executes actions with streaming TTS.
    Speaks each sentence as it's generated — not after full response.
    """

    def __init__(self):
        self._is_streaming = False
        self._client = get_client()

    async def execute_with_stream(
        self,
        task: str,
        context: str = "",
        on_chunk: Optional[Callable[[str], None]] = None,
    ) -> str:
        """
        Execute task with streaming TTS.

        Args:
            task: What to do / what to say
            context: Additional context
            on_chunk: Optional callback for each text chunk (for logging/UI)

        Returns:
            Full response text
        """
        from brain.llm import LLMResponse, TextBlock

        self._is_streaming = True
        full_response: list[str] = []
        pending_buffer = ""

        try:
            # Use the client's streaming method if available
            if hasattr(self._client, "stream"):
                async for chunk in self._client.stream(
                    messages=[
                        {
                            "role": "user",
                            "content": f"{task}\n\n{context}" if context else task,
                        }
                    ],
                    system="",
                ):
                    if isinstance(chunk, str):
                        full_response.append(chunk)
                        pending_buffer += chunk
                    elif isinstance(chunk, TextBlock):
                        full_response.append(chunk.text)
                        pending_buffer += chunk.text

                    if on_chunk:
                        on_chunk(chunk)

                    # Check for sentence boundary — speak the chunk
                    if _should_flush(pending_buffer):
                        chunk_to_speak = pending_buffer.strip()
                        pending_buffer = ""

                        if config.VOICE_ENABLED and chunk_to_speak:
                            asyncio.create_task(speak(chunk_to_speak))

            # Flush any remaining text
            if pending_buffer.strip():
                await speak(pending_buffer.strip())

        except Exception as e:
            log.error(f"Streaming execution error: {e}")
        finally:
            self._is_streaming = False

        return "".join(full_response)


def _should_flush(text: str) -> bool:
    """
    Decide if we should speak the buffered text now.
    Flush on sentence boundaries, or when buffer is getting long.
    """
    if len(text) < _MIN_CHUNK_CHARS:
        return False
    # Has sentence-ending punctuation
    if _SENTENCE_END.search(text):
        return True
    # Buffer is long enough to speak even without punctuation
    if len(text) >= 150:
        return True
    return False


# ─── Module-level convenience ──────────────────────────────────────────────────

_executor: Optional[StreamingExecutor] = None


def get_streaming_executor() -> StreamingExecutor:
    global _executor
    if _executor is None:
        _executor = StreamingExecutor()
    return _executor


async def execute_with_streaming(task: str, context: str = "") -> str:
    """Execute a task with streaming TTS response."""
    return await get_streaming_executor().execute_with_stream(task, context)


async def speak_streaming(text: str) -> None:
    """
    Speak pre-generated text in sentence chunks for lower perceived latency.
    Useful when you have a long response and want to start speaking immediately.
    """
    sentences = _SENTENCE_END.split(text)
    for sentence in sentences:
        sentence = sentence.strip()
        if sentence:
            await speak(sentence)


def cancel_stream() -> None:
    """Cancel ongoing streaming speech."""
    cancel_speaking()


async def execute_action_streaming(
    task: str,
    context: str = "",
    stream: bool = False,
) -> str:
    """Execute action, optionally with streaming TTS."""
    if stream:
        return await execute_with_streaming(task, context)
    from actions.executor import execute_action

    return await execute_action(task, context)
