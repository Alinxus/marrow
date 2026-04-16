import os
from pathlib import Path
from dotenv import load_dotenv

# Canonical env file used by Settings panel + runtime.
# Override with MARROW_ENV_FILE if needed.
ENV_FILE = Path(
    os.environ.get("MARROW_ENV_FILE", str(Path.home() / ".marrow" / ".env"))
)

if ENV_FILE.exists():
    load_dotenv(ENV_FILE, override=True)
else:
    # Fallback for dev/local runs where only project .env exists.
    load_dotenv(override=True)

TOKEN_SAVER_MODE = os.environ.get("TOKEN_SAVER_MODE", "0") == "1"

# ─── LLM Provider ─────────────────────────────────────────────────────────────
# Set LLM_PROVIDER to: "auto" | "anthropic" | "openai" | "ollama" | "none"
# auto = prefer configured cloud key, otherwise local Ollama, otherwise no-LLM mode
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "auto")

# Anthropic (optional - used if LLM_PROVIDER=anthropic and key provided)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
REASONING_MODEL = os.environ.get("REASONING_MODEL", "claude-sonnet-4-6")
SCORING_MODEL = os.environ.get("SCORING_MODEL", "claude-haiku-4-5-20251001")
VISION_MODEL = os.environ.get("VISION_MODEL", "claude-haiku-4-5-20251001")

# OpenAI (optional - used if LLM_PROVIDER=openai and key provided)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_REASONING_MODEL = os.environ.get("OPENAI_REASONING_MODEL", "gpt-5.4-mini")
OPENAI_SCORING_MODEL = os.environ.get("OPENAI_SCORING_MODEL", "gpt-5.4-mini")
OPENAI_VISION_MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-5.4-mini")

# Ollama (local - no API key needed)
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_REASONING_MODEL = os.environ.get("OLLAMA_REASONING_MODEL", "llama3.2")
OLLAMA_SCORING_MODEL = os.environ.get("OLLAMA_SCORING_MODEL", "llama3.2")
OLLAMA_VISION_MODEL = os.environ.get("OLLAMA_VISION_MODEL", "llava")

# ─── Voice & Identity ─────────────────────────────────────────────────────────
# Voice is optional - if empty, Marrow will run silently
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
MARROW_NAME = os.environ.get("MARROW_NAME", "Marrow")
MARROW_VOICE_ID = os.environ.get("MARROW_VOICE_ID", "BAMYoBHLZM7lJgJAmFz0")
VOICE_ENABLED = bool(ELEVENLABS_API_KEY)

# ─── Intervals ────────────────────────────────────────────────────────────────
REASONING_INTERVAL = int(os.environ.get("REASONING_INTERVAL", "25"))
INTERRUPT_COOLDOWN = int(os.environ.get("INTERRUPT_COOLDOWN", "90"))
SCREENSHOT_INTERVAL = int(os.environ.get("SCREENSHOT_INTERVAL", "4"))
CONTEXT_WINDOW_SECONDS = int(os.environ.get("CONTEXT_WINDOW_SECONDS", "300"))
AUDIO_CHUNK_SECONDS = int(os.environ.get("AUDIO_CHUNK_SECONDS", "5"))
AUDIO_INPUT_DEVICE = os.environ.get("AUDIO_INPUT_DEVICE", "")

# Audio — "small" is the minimum for decent accuracy; "medium" is better but slower
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
# Audio STT backend: auto | whisper | deepgram | none
AUDIO_STT_BACKEND = os.environ.get("AUDIO_STT_BACKEND", "auto").lower()

# RetainDB
RETAINDB_API_KEY = os.environ.get("RETAINDB_API_KEY", "")
RETAINDB_PROJECT = os.environ.get("RETAINDB_PROJECT", "marrow")

# Deepgram — if set, replaces Whisper with real-time streaming (much better)
DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
SILENCE_THRESHOLD = float(os.environ.get("SILENCE_THRESHOLD", "0.01"))

# Actions
MAX_ACTION_ITERATIONS = int(os.environ.get("MAX_ACTION_ITERATIONS", "8"))
AUTO_COMPLEX_ESCALATION = os.environ.get("AUTO_COMPLEX_ESCALATION", "1") == "1"
ADAPTER_AUTO_LEARN = os.environ.get("ADAPTER_AUTO_LEARN", "1") == "1"
ADAPTER_SUGGEST_THRESHOLD = int(os.environ.get("ADAPTER_SUGGEST_THRESHOLD", "3"))
ADAPTER_MIN_TRUST_TO_RECOMMEND = float(
    os.environ.get("ADAPTER_MIN_TRUST_TO_RECOMMEND", "0.35")
)
ACTION_CHAT_HISTORY_MESSAGES = int(os.environ.get("ACTION_CHAT_HISTORY_MESSAGES", "12"))
ACTION_CHAT_HISTORY_CHARS = int(os.environ.get("ACTION_CHAT_HISTORY_CHARS", "1600"))

# Proactive interruption policy (Omi-style gating)
# 1 = very strict, 5 = very frequent
PROACTIVE_FREQUENCY = int(os.environ.get("PROACTIVE_FREQUENCY", "3"))
MAX_DAILY_INTERRUPTS = int(os.environ.get("MAX_DAILY_INTERRUPTS", "12"))

# Apps that signal deep-focus / flow state (interrupt less aggressively)
FLOW_STATE_APPS = [
    "code",
    "cursor",
    "vim",
    "nvim",
    "emacs",
    "idea",
    "pycharm",
    "sublime_text",
    "notepad++",
    "terminal",
    "windowsterminal",
    "cmd",
    "wt",  # Windows Terminal
]

# Apps that signal active meeting (soft-mute non-urgent interrupts)
MEETING_APPS = [
    "zoom",
    "teams",
    "slack",
    "meet",
    "webex",
    "discord",
    "whereby",
    "loom",
]

# Screenshot storage
SCREENSHOT_SAVE_TO_DISK = os.environ.get("SCREENSHOT_SAVE_TO_DISK", "1") == "1"
SCREEN_VISION_MAX_SIZE = int(os.environ.get("SCREEN_VISION_MAX_SIZE", "1920"))
SCREEN_VISION_JPEG_QUALITY = int(os.environ.get("SCREEN_VISION_JPEG_QUALITY", "85"))

# Token controls
VISION_MAX_TOKENS = int(
    os.environ.get("VISION_MAX_TOKENS", "320" if TOKEN_SAVER_MODE else "700")
)
SCREEN_VISION_INTERVAL_SECONDS = int(
    os.environ.get("SCREEN_VISION_INTERVAL_SECONDS", "12" if TOKEN_SAVER_MODE else "4")
)
REASONING_MAX_TOKENS = int(
    os.environ.get("REASONING_MAX_TOKENS", "360" if TOKEN_SAVER_MODE else "600")
)
WORLD_MODEL_MAX_TOKENS = int(
    os.environ.get("WORLD_MODEL_MAX_TOKENS", "220" if TOKEN_SAVER_MODE else "512")
)
GATE_MAX_TOKENS = int(
    os.environ.get("GATE_MAX_TOKENS", "80" if TOKEN_SAVER_MODE else "120")
)
CRITIC_MAX_TOKENS = int(
    os.environ.get("CRITIC_MAX_TOKENS", "80" if TOKEN_SAVER_MODE else "120")
)
FOUR_AXIS_MAX_TOKENS = int(
    os.environ.get("FOUR_AXIS_MAX_TOKENS", "90" if TOKEN_SAVER_MODE else "150")
)
REASONING_CONTEXT_CHAR_LIMIT = int(
    os.environ.get(
        "REASONING_CONTEXT_CHAR_LIMIT", "3600" if TOKEN_SAVER_MODE else "7000"
    )
)
GATE_CONTEXT_CHAR_LIMIT = int(
    os.environ.get("GATE_CONTEXT_CHAR_LIMIT", "1400" if TOKEN_SAVER_MODE else "2200")
)
MEMORY_REFRESH_CYCLES = int(
    os.environ.get("MEMORY_REFRESH_CYCLES", "3" if TOKEN_SAVER_MODE else "1")
)

# System tray
TRAY_ENABLED = os.environ.get("TRAY_ENABLED", "1") == "1"

# UI mode
# orb = small always-on orb + optional dashboard
# controlbar = unified floating control bar
UI_MODE = os.environ.get("UI_MODE", "orb").lower()
CONTROL_BAR_AUTO_SHOW = os.environ.get("CONTROL_BAR_AUTO_SHOW", "0") == "1"

# On-demand activation
ON_DEMAND_HOTKEY = os.environ.get("ON_DEMAND_HOTKEY", "ctrl+shift+m")
HOTKEY_ENABLED = os.environ.get("HOTKEY_ENABLED", "1") == "1"
WAKE_WORD_ENABLED = os.environ.get("WAKE_WORD_ENABLED", "1") == "1"
WAKE_WORDS = ["marrow", "hey marrow"]

# Audio capture - set to false if no microphone
AUDIO_ENABLED = os.environ.get("AUDIO_ENABLED", "1") == "1"


def get_browser_llm():
    """
    Get LLM client for browser-use.
    browser-use requires langchain-anthropic or langchain-openai, NOT the
    browser_use package itself for LLM classes.
    Install: pip install langchain-anthropic
    """
    try:
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model="claude-haiku-4-5-20251001",
            anthropic_api_key=ANTHROPIC_API_KEY,
        )
    except ImportError:
        pass

    try:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model="gpt-4o-mini")
    except ImportError:
        pass

    import logging

    logging.getLogger(__name__).warning(
        "browser-use LLM unavailable. Install: pip install langchain-anthropic"
    )
    return None
