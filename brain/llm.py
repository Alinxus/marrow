"""
Unified LLM client — Anthropic, OpenAI, or Ollama.

All modules use get_client() instead of importing anthropic/openai directly.
Responses are normalized to Anthropic's content-block shape so existing code
works unchanged regardless of provider.

Tool-use loop is encapsulated in create_with_tools() — callers don't manage
message history or stop-reason checks themselves.
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

import config

log = logging.getLogger(__name__)


# ─── Normalized response types ────────────────────────────────────────────────


@dataclass
class TextBlock:
    type: str = "text"
    text: str = ""

    def __str__(self) -> str:
        return self.text


@dataclass
class ToolUseBlock:
    type: str = "tool_use"
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class LLMResponse:
    """Normalized response — same shape as Anthropic's for all providers."""

    content: list  # list[TextBlock | ToolUseBlock]
    stop_reason: str  # "end_turn" | "tool_use"
    model: str = ""

    @property
    def text(self) -> str:
        """First text block, or empty string."""
        for block in self.content:
            if isinstance(block, TextBlock):
                return block.text
        return ""


# ─── Conversion helpers ───────────────────────────────────────────────────────


def _tools_to_openai(tools: list[dict]) -> list[dict]:
    """Anthropic tool schema → OpenAI function schema."""
    result = []
    for t in tools:
        result.append(
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get(
                        "input_schema", {"type": "object", "properties": {}}
                    ),
                },
            }
        )
    return result


def _messages_to_openai(messages: list[dict]) -> list[dict]:
    """
    Convert an Anthropic-format message history to OpenAI format.

    Handles:
    - Plain string content
    - Tool-use assistant messages (list with ToolUseBlock)
    - Tool-result user messages  (list with {"type": "tool_result", ...})
    """
    result = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if isinstance(content, str):
            result.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            result.append({"role": role, "content": str(content)})
            continue

        # ── User multimodal content passthrough (text + image_url blocks) ──
        if role == "user" and all(isinstance(b, dict) and "type" in b for b in content):
            has_media = any(
                b.get("type") in ("image_url", "input_image") for b in content
            )
            if has_media:
                normalized_blocks = []
                for b in content:
                    btype = b.get("type")
                    if btype in ("text", "input_text"):
                        normalized_blocks.append(
                            {"type": "text", "text": b.get("text", "")}
                        )
                    elif btype in ("image_url", "input_image"):
                        normalized_blocks.append(
                            {
                                "type": "image_url",
                                "image_url": b.get("image_url", {}),
                            }
                        )
                result.append({"role": "user", "content": normalized_blocks})
                continue

        # ── Tool result messages → OpenAI "tool" role ──────────────────────
        if any(
            (isinstance(b, dict) and b.get("type") == "tool_result") for b in content
        ):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    c = item.get("content", "")
                    result.append(
                        {
                            "role": "tool",
                            "tool_call_id": item["tool_use_id"],
                            "content": c if isinstance(c, str) else json.dumps(c),
                        }
                    )
            continue

        # ── Assistant message with tool-use blocks ─────────────────────────
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        for block in content:
            if isinstance(block, TextBlock):
                if block.text:
                    text_parts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                tool_calls.append(
                    {
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": json.dumps(block.input),
                        },
                    }
                )
            elif isinstance(block, dict):
                btype = block.get("type", "")
                if btype == "text" and block.get("text"):
                    text_parts.append(block["text"])
                elif btype == "tool_use":
                    tool_calls.append(
                        {
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block["name"],
                                "arguments": json.dumps(block.get("input", {})),
                            },
                        }
                    )

        oai_msg: dict = {"role": role}
        oai_msg["content"] = " ".join(text_parts) if text_parts else None
        if tool_calls:
            oai_msg["tool_calls"] = tool_calls
        result.append(oai_msg)

    return result


# ─── Client ───────────────────────────────────────────────────────────────────


class LLMClient:
    """
    Unified async LLM client for Anthropic, OpenAI, and Ollama.

    Usage:
        client = get_client()
        response = await client.create(messages, system=prompt)
        text = await client.create_with_tools(task, tools, tool_handler)
    """

    def __init__(self, provider: str = None):
        requested = (provider or config.LLM_PROVIDER or "auto").lower().strip()
        self.requested = requested
        self.provider = self._resolve_provider(requested)
        self._anthropic = None
        self._openai = None
        log.info(f"LLM provider resolved: requested={requested} active={self.provider}")

    def _ollama_available(self) -> bool:
        try:
            r = httpx.get(f"{config.OLLAMA_BASE_URL}/api/tags", timeout=1.2)
            return r.status_code == 200
        except Exception:
            return False

    def _resolve_provider(self, requested: str) -> str:
        """Resolve provider safely. Never raises on missing keys."""
        if requested == "none":
            self._resolution_reason = "requested none"
            return "none"

        if requested == "anthropic":
            if config.ANTHROPIC_API_KEY:
                self._resolution_reason = "anthropic key present"
                return "anthropic"
            self._resolution_reason = "ANTHROPIC_API_KEY missing"
            log.warning("ANTHROPIC_API_KEY missing; falling back")

        if requested == "openai":
            if config.OPENAI_API_KEY:
                self._resolution_reason = "openai key present"
                return "openai"
            self._resolution_reason = "OPENAI_API_KEY missing"
            log.warning("OPENAI_API_KEY missing; falling back")

        if requested == "ollama":
            if self._ollama_available():
                self._resolution_reason = "ollama available"
                return "ollama"
            self._resolution_reason = "ollama unavailable"
            log.warning("Ollama unavailable; falling back")

        # auto / fallback chain
        if config.OPENAI_API_KEY:
            self._resolution_reason = "auto selected openai"
            return "openai"
        if config.ANTHROPIC_API_KEY:
            self._resolution_reason = "auto selected anthropic"
            return "anthropic"
        if self._ollama_available():
            self._resolution_reason = "auto selected ollama"
            return "ollama"

        self._resolution_reason = "no API keys or local LLM available"
        log.warning("No LLM backend available. Running in no-LLM mode.")
        return "none"

    def status(self) -> dict[str, Any]:
        """Return a compact provider status snapshot for logs/UI."""
        return {
            "requested": self.requested,
            "resolved": self.provider,
            "reason": getattr(self, "_resolution_reason", "unknown"),
            "openai_key": bool(config.OPENAI_API_KEY),
            "anthropic_key": bool(config.ANTHROPIC_API_KEY),
            "ollama_available": self._ollama_available(),
        }

    # ── Lazy backends ──────────────────────────────────────────────────────

    def _get_anthropic(self):
        if self._anthropic is None:
            import anthropic as _a

            self._anthropic = _a.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
        return self._anthropic

    def _get_openai(self):
        if self._openai is None:
            import openai as _o

            if self.provider == "ollama":
                self._openai = _o.AsyncOpenAI(
                    base_url=f"{config.OLLAMA_BASE_URL}/v1",
                    api_key="ollama",
                )
            else:
                self._openai = _o.AsyncOpenAI(api_key=config.OPENAI_API_KEY)
        return self._openai

    # ── Model resolution ───────────────────────────────────────────────────

    def model_for(self, model_type: str = "reasoning") -> str:
        """Return the correct model name string for this provider + type."""
        p = self.provider
        if p == "anthropic":
            if model_type == "vision":
                return config.VISION_MODEL
            return (
                config.REASONING_MODEL
                if model_type == "reasoning"
                else config.SCORING_MODEL
            )
        elif p == "openai":
            if model_type == "vision":
                return config.OPENAI_VISION_MODEL
            return (
                config.OPENAI_REASONING_MODEL
                if model_type == "reasoning"
                else config.OPENAI_SCORING_MODEL
            )
        elif p == "ollama":
            if model_type == "vision":
                return config.OLLAMA_VISION_MODEL
            return (
                config.OLLAMA_REASONING_MODEL
                if model_type == "reasoning"
                else config.OLLAMA_SCORING_MODEL
            )
        elif p == "none":
            return "none"
        return config.REASONING_MODEL

    def supports_streaming(self) -> bool:
        return self.provider == "anthropic" and bool(config.ANTHROPIC_API_KEY)

    def get_raw_anthropic(self):
        """Return the raw Anthropic async client (for streaming TTS path)."""
        if self.provider != "anthropic":
            return None
        return self._get_anthropic()

    # ── Single-turn create ─────────────────────────────────────────────────

    async def create(
        self,
        messages: list[dict],
        system: str = "",
        tools: list[dict] = None,
        max_tokens: int = 1024,
        model_type: str = "reasoning",
        model: str = None,
        max_completion_tokens: int = None,
    ) -> LLMResponse:
        """Single LLM call — returns normalized LLMResponse."""
        if self.provider == "none":
            return LLMResponse(
                content=[TextBlock(text="")], stop_reason="end_turn", model="none"
            )

        model = model or self.model_for(model_type)
        if self.provider == "anthropic":
            return await self._anthropic_create(
                messages, system, tools, max_tokens, model
            )
        else:
            return await self._openai_create(
                messages, system, tools, max_tokens, model, max_completion_tokens
            )

    async def _anthropic_create(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        max_tokens: int,
        model: str,
    ) -> LLMResponse:
        client = self._get_anthropic()
        import anthropic as _a

        kwargs: dict = dict(model=model, max_tokens=max_tokens, messages=messages)
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        resp = await client.messages.create(**kwargs)

        content: list = []
        for block in resp.content:
            if hasattr(block, "text"):
                content.append(TextBlock(text=block.text))
            elif block.type == "tool_use":
                content.append(
                    ToolUseBlock(id=block.id, name=block.name, input=block.input)
                )

        return LLMResponse(
            content=content, stop_reason=resp.stop_reason, model=resp.model
        )

    async def _openai_create(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        max_tokens: int,
        model: str,
        max_completion_tokens: int = None,
    ) -> LLMResponse:
        client = self._get_openai()

        oai_messages: list[dict] = []
        if system:
            oai_messages.append({"role": "system", "content": system})
        oai_messages.extend(_messages_to_openai(messages))

        kwargs: dict = dict(
            model=model,
            max_completion_tokens=max_completion_tokens
            if max_completion_tokens
            else max_tokens,
            messages=oai_messages,
        )
        if tools:
            kwargs["tools"] = _tools_to_openai(tools)
            kwargs["tool_choice"] = "auto"

        resp = await client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        finish = resp.choices[0].finish_reason

        stop_reason = "tool_use" if finish == "tool_calls" else "end_turn"
        content: list = []
        if msg.content:
            content.append(TextBlock(text=msg.content))
        if msg.tool_calls:
            for tc in msg.tool_calls:
                content.append(
                    ToolUseBlock(
                        id=tc.id,
                        name=tc.function.name,
                        input=json.loads(tc.function.arguments or "{}"),
                    )
                )

        return LLMResponse(content=content, stop_reason=stop_reason, model=model)

    # ── Tool-use loop ──────────────────────────────────────────────────────

    async def create_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        tool_handler: Callable,  # async (name: str, input: dict) -> str
        system: str = "",
        max_tokens: int = 1024,
        model_type: str = "reasoning",
        model: str = None,
        max_iterations: int = 8,
        on_tool_call: Optional[Callable] = None,  # async (name, input, result) -> None
    ) -> str:
        """
        Full tool-use loop — calls LLM, handles tool calls, returns final text.

        Manages message history internally so callers don't deal with
        provider-specific formats.

        Args:
            messages:       initial messages (user content)
            tools:          tool definitions (Anthropic schema format)
            tool_handler:   async callable(name, input) -> result string
            on_tool_call:   optional hook for logging / UI updates
            max_iterations: safety limit

        Returns:
            Final assistant text response.
        """
        model = model or self.model_for(model_type)

        # We keep a mutable copy of the message history
        history: list[dict] = list(messages)

        for iteration in range(max_iterations):
            # Use max_completion_tokens for OpenAI (newer models require it)
            response = await self.create(
                messages=history,
                system=system,
                tools=tools,
                max_tokens=max_tokens,
                model=model,
                max_completion_tokens=max_tokens,
            )

            if response.stop_reason == "end_turn":
                return response.text

            if response.stop_reason != "tool_use":
                break  # unexpected stop

            # ── Append assistant turn ──────────────────────────────────────
            # Store as list of our normalized blocks (the Anthropic provider
            # re-sends these; _messages_to_openai handles the conversion for OAI)
            history.append({"role": "assistant", "content": response.content})

            # ── Execute all tool calls in this turn ────────────────────────
            tool_results: list[dict] = []
            for block in response.content:
                if not isinstance(block, ToolUseBlock):
                    continue

                try:
                    result = await tool_handler(block.name, block.input)
                except Exception as e:
                    result = f"[tool error: {e}]"
                    log.error(f"Tool {block.name} error: {e}")

                if on_tool_call:
                    try:
                        await on_tool_call(block.name, block.input, result)
                    except Exception:
                        pass

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    }
                )

            history.append({"role": "user", "content": tool_results})

        log.warning(f"Tool loop hit max_iterations ({max_iterations})")
        return response.text if response else ""


# ─── Module-level singleton ────────────────────────────────────────────────────

_client: Optional[LLMClient] = None


def get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


def reset_client() -> None:
    """Force recreation after settings change."""
    global _client
    _client = None
