"""
Context compression for long conversations.

When conversation gets too long, summarizes old messages to save tokens.
Uses the same LLM to compress, keeps most recent + important info.
"""

import asyncio
import logging
from typing import List, Dict, Any

import anthropic

import config

log = logging.getLogger(__name__)


# Keep this many most recent messages (protected)
PROTECT_LAST_N = 6
# Keep this many oldest messages (protected)
PROTECT_FIRST_N = 2
# Compress when over this many tokens (approx)
TOKEN_THRESHOLD = 80000
# Max tokens for compressed summary
SUMMARY_MAX_TOKENS = 2000


class ContextCompressor:
    """
    Compresses long conversation history while preserving important info.

    Strategy:
    1. Protect first N and last N messages (important context)
    2. Compress middle messages into a summary
    3. Replace middle with the summary
    """

    def __init__(self, threshold: int = TOKEN_THRESHOLD):
        self.threshold = threshold
        self.compression_count = 0

    async def should_compress(self, messages: List[Dict]) -> bool:
        """Check if compression is needed."""
        # Count messages
        if len(messages) > 20:
            return True
        return False

    async def compress(
        self,
        messages: List[Dict],
        system_prompt: str = "",
    ) -> List[Dict]:
        """
        Compress messages in the middle.

        Protected: first 2 messages + last 6 messages
        Compressed: everything else
        """

        if len(messages) <= PROTECT_LAST_N + PROTECT_FIRST_N:
            return messages

        # Split into protected and compressible
        first = messages[:PROTECT_FIRST_N]
        middle = messages[PROTECT_FIRST_N:-PROTECT_LAST_N]
        last = messages[-PROTECT_LAST_N:]

        if not middle:
            return messages

        # Build summary prompt
        middle_text = self._format_messages(middle)

        summary = await self._summarize_messages(middle_text)

        # Create compressed messages
        compressed = [
            *first,
            {
                "role": "assistant",
                "content": f"[Earlier conversation summarized]\n{summary}",
            },
            *last,
        ]

        self.compression_count += 1
        log.info(f"Context compressed: {len(messages)} → {len(compressed)} messages")

        return compressed

    def _format_messages(self, messages: List[Dict]) -> str:
        """Format messages for summary."""
        lines = []
        for m in messages:
            role = m.get("role", "unknown")
            content = m.get("content", "")
            if isinstance(content, list):
                # Has tool calls
                content = f"[tool calls: {len(content)}]"
            lines.append(f"{role.upper()}: {content[:500]}")
        return "\n\n".join(lines)

    async def _summarize_messages(self, text: str) -> str:
        """Summarize the middle messages."""
        client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

        try:
            response = await client.messages.create(
                model=config.SCORING_MODEL,
                max_tokens=SUMMARY_MAX_TOKENS,
                messages=[
                    {
                        "role": "user",
                        "content": f"""Summarize this conversation concisely. Include:
- Key topics discussed
- Important decisions made
- Any tasks or actions mentioned
- User preferences or facts mentioned

Conversation:
{text}

Summary (be concise, use bullet points):""",
                    }
                ],
            )

            return response.content[0].text.strip()

        except Exception as e:
            log.warning(f"Compression summary failed: {e}")
            return "[Earlier conversation - details not preserved]"


# Global compressor
_compressor: ContextCompressor = None


def get_compressor() -> ContextCompressor:
    global _compressor
    if _compressor is None:
        _compressor = ContextCompressor()
    return _compressor


async def compress_if_needed(messages: List[Dict]) -> List[Dict]:
    """Convenience function to compress if needed."""
    compressor = get_compressor()

    if await compressor.should_compress(messages):
        return await compressor.compress(messages)

    return messages
