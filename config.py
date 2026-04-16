import os
from dotenv import load_dotenv

load_dotenv()

# ─── LLM Provider ─────────────────────────────────────────────────────────────
# Set LLM_PROVIDER to: "anthropic" | "openai" | "ollama"
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai")

# Anthropic (optional - used if LLM_PROVIDER=anthropic and key provided)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
REASONING_MODEL = "claude-sonnet-4-6"
SCORING_MODEL = "claude-haiku-4-5-20251001"
VISION_MODEL = "claude-haiku-4-5-20251001"

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

# Audio — "small" is the minimum for decent accuracy; "medium" is better but slower
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")

# RetainDB
RETAINDB_API_KEY = os.environ.get("RETAINDB_API_KEY", "")
RETAINDB_PROJECT = os.environ.get("RETAINDB_PROJECT", "marrow")

# Deepgram — if set, replaces Whisper with real-time streaming (much better)
DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
SILENCE_THRESHOLD = float(os.environ.get("SILENCE_THRESHOLD", "0.01"))

# Actions
MAX_ACTION_ITERATIONS = int(os.environ.get("MAX_ACTION_ITERATIONS", "8"))

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

# System tray
TRAY_ENABLED = os.environ.get("TRAY_ENABLED", "1") == "1"

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
