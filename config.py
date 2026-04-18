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
# Voice is optional - local/system TTS can still work without ElevenLabs.
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
MARROW_NAME = os.environ.get("MARROW_NAME", "Marrow")
MARROW_VOICE_ID = os.environ.get("MARROW_VOICE_ID", "BAMYoBHLZM7lJgJAmFz0")
VOICE_ENABLED = os.environ.get("VOICE_ENABLED", "1") == "1"

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
RETAINDB_CONTEXT_REFRESH_SECONDS = int(
    os.environ.get("RETAINDB_CONTEXT_REFRESH_SECONDS", "75")
)
RETAINDB_PROFILE_REFRESH_SECONDS = int(
    os.environ.get("RETAINDB_PROFILE_REFRESH_SECONDS", "300")
)

# Deepgram — if set, replaces Whisper with real-time streaming (much better)
DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
DEEPGRAM_MODEL = os.environ.get("DEEPGRAM_MODEL", "nova-3")
DEEPGRAM_LANGUAGE = os.environ.get("DEEPGRAM_LANGUAGE", "en")
DEEPGRAM_ENDPOINTING_MS = int(os.environ.get("DEEPGRAM_ENDPOINTING_MS", "180"))
DEEPGRAM_UTTERANCE_END_MS = int(os.environ.get("DEEPGRAM_UTTERANCE_END_MS", "700"))
DEEPGRAM_RECONNECT_BASE_SECONDS = float(
    os.environ.get("DEEPGRAM_RECONNECT_BASE_SECONDS", "1.0")
)
DEEPGRAM_VAD_GATE_ENABLED = os.environ.get("DEEPGRAM_VAD_GATE_ENABLED", "1") != "0"
DEEPGRAM_VAD_HANGOVER_MS = int(os.environ.get("DEEPGRAM_VAD_HANGOVER_MS", "650"))
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
PROACTIVE_FREQUENCY = int(os.environ.get("PROACTIVE_FREQUENCY", "4"))

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
SCREEN_OCR_ENABLED = os.environ.get("SCREEN_OCR_ENABLED", "1") == "1"
SCREEN_OCR_MAX_CHARS = int(os.environ.get("SCREEN_OCR_MAX_CHARS", "1800"))

# Token controls
VISION_MAX_TOKENS = int(
    os.environ.get("VISION_MAX_TOKENS", "420" if TOKEN_SAVER_MODE else "1200")
)
SCREEN_VISION_INTERVAL_SECONDS = int(
    os.environ.get("SCREEN_VISION_INTERVAL_SECONDS", "8" if TOKEN_SAVER_MODE else "2")
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
        "REASONING_CONTEXT_CHAR_LIMIT", "5200" if TOKEN_SAVER_MODE else "12000"
    )
)
GATE_CONTEXT_CHAR_LIMIT = int(
    os.environ.get("GATE_CONTEXT_CHAR_LIMIT", "2200" if TOKEN_SAVER_MODE else "4000")
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

# Conversational mode
CONVERSATION_ENABLED = os.environ.get("CONVERSATION_ENABLED", "1") == "1"
CONVERSATION_MODE_TIMEOUT_SECONDS = int(
    os.environ.get("CONVERSATION_MODE_TIMEOUT_SECONDS", "120")
)
CONVERSATION_MAX_TURNS = int(os.environ.get("CONVERSATION_MAX_TURNS", "20"))
CONVERSATION_MAX_TOKENS = int(os.environ.get("CONVERSATION_MAX_TOKENS", "320"))
CONVERSATION_MODEL_TYPE = os.environ.get("CONVERSATION_MODEL_TYPE", "reasoning").lower()
CONVERSATION_CONTEXT_CHAR_LIMIT = int(
    os.environ.get("CONVERSATION_CONTEXT_CHAR_LIMIT", "1400")
)
CONVERSATION_FAST_PATH_ENABLED = (
    os.environ.get("CONVERSATION_FAST_PATH_ENABLED", "1") != "0"
)
CONVERSATION_RESPONSE_STYLE = os.environ.get(
    "CONVERSATION_RESPONSE_STYLE", "balanced"
).lower()

# Smart home (optional Home Assistant bridge)
HOME_ASSISTANT_URL = os.environ.get("HOME_ASSISTANT_URL", "")
HOME_ASSISTANT_TOKEN = os.environ.get("HOME_ASSISTANT_TOKEN", "")

# Audio capture - set to false if no microphone
AUDIO_ENABLED = os.environ.get("AUDIO_ENABLED", "1") == "1"
AUDIO_ACTIVE_CHUNK_SECONDS = int(os.environ.get("AUDIO_ACTIVE_CHUNK_SECONDS", "1"))
AUDIO_MIN_TRANSCRIPT_CHARS = int(os.environ.get("AUDIO_MIN_TRANSCRIPT_CHARS", "3"))

# Mission mode / orchestration
MISSION_ENABLED = os.environ.get("MISSION_ENABLED", "1") == "1"
MISSION_AUTO_VERIFY = os.environ.get("MISSION_AUTO_VERIFY", "1") == "1"
MISSION_MAX_STEPS = int(os.environ.get("MISSION_MAX_STEPS", "8"))
MISSION_STEP_TIMEOUT_SECONDS = int(os.environ.get("MISSION_STEP_TIMEOUT_SECONDS", "90"))
MISSION_RECOVERY_ENABLED = os.environ.get("MISSION_RECOVERY_ENABLED", "1") == "1"

# Overlay
OVERLAY_ENABLED = (
    os.environ.get("OVERLAY_ENABLED", "0" if UI_MODE == "orb" else "1") == "1"
)
OVERLAY_AUTO_HIDE_FULLSCREEN = (
    os.environ.get("OVERLAY_AUTO_HIDE_FULLSCREEN", "1") == "1"
)

# Swarm / predictive / proactive
SWARM_ENABLED = os.environ.get("SWARM_ENABLED", "1") == "1"
SWARM_MAX_AGENTS = int(os.environ.get("SWARM_MAX_AGENTS", "3"))
PREDICTIVE_ENABLED = os.environ.get("PREDICTIVE_ENABLED", "1") == "1"
PREDICTIVE_INTERVAL_SECONDS = int(os.environ.get("PREDICTIVE_INTERVAL_SECONDS", "90"))
PROACTIVE_SPEECH_ENABLED = os.environ.get("PROACTIVE_SPEECH_ENABLED", "1") == "1"
PROACTIVE_SPEECH_MIN_URGENCY = int(os.environ.get("PROACTIVE_SPEECH_MIN_URGENCY", "2"))
PROACTIVE_SPEECH_MIN_GAP_SECONDS = int(
    os.environ.get("PROACTIVE_SPEECH_MIN_GAP_SECONDS", "30")
)
PROACTIVE_SIGNAL_DEDUP_SECONDS = int(
    os.environ.get("PROACTIVE_SIGNAL_DEDUP_SECONDS", "180")
)
PROACTIVE_AUTO_SPEAK_MIN_URGENCY = int(
    os.environ.get("PROACTIVE_AUTO_SPEAK_MIN_URGENCY", "2")
)
PROACTIVE_TOAST_MIN_URGENCY = int(os.environ.get("PROACTIVE_TOAST_MIN_URGENCY", "1"))
PROACTIVE_FORCE_TOAST_WHEN_AUDIO_UNAVAILABLE = (
    os.environ.get("PROACTIVE_FORCE_TOAST_WHEN_AUDIO_UNAVAILABLE", "1") == "1"
)
PROACTIVE_STARTUP_DELAY_SECONDS = int(
    os.environ.get("PROACTIVE_STARTUP_DELAY_SECONDS", "8")
)
PROACTIVE_BACKOFF_MAX_SECONDS = int(
    os.environ.get("PROACTIVE_BACKOFF_MAX_SECONDS", "300")
)
PROACTIVE_AMBIENT_PULSE_ENABLED = (
    os.environ.get("PROACTIVE_AMBIENT_PULSE_ENABLED", "0") == "1"
)
PROACTIVE_PRESENCE_PING_ENABLED = (
    os.environ.get("PROACTIVE_PRESENCE_PING_ENABLED", "0") == "1"
)

# Mentor-style buffered proactive lane (ported pattern from Omi)
MENTOR_PROACTIVE_ENABLED = os.environ.get("MENTOR_PROACTIVE_ENABLED", "0") == "1"
MENTOR_CONTEXT_WINDOW_SECONDS = int(
    os.environ.get("MENTOR_CONTEXT_WINDOW_SECONDS", "240")
)
MENTOR_MAX_BUFFER_MESSAGES = int(os.environ.get("MENTOR_MAX_BUFFER_MESSAGES", "50"))
MENTOR_MIN_NEW_SEGMENTS_FOR_ANALYSIS = int(
    os.environ.get("MENTOR_MIN_NEW_SEGMENTS_FOR_ANALYSIS", "6")
)
MENTOR_SILENCE_RESET_SECONDS = int(
    os.environ.get("MENTOR_SILENCE_RESET_SECONDS", "120")
)
MENTOR_MIN_WORDS_AFTER_SILENCE = int(
    os.environ.get("MENTOR_MIN_WORDS_AFTER_SILENCE", "5")
)
MENTOR_MIN_TRANSCRIPT_CHARS = int(os.environ.get("MENTOR_MIN_TRANSCRIPT_CHARS", "3"))
MENTOR_RATE_LIMIT_SECONDS = int(os.environ.get("MENTOR_RATE_LIMIT_SECONDS", "120"))
MENTOR_MAX_DAILY_NOTIFICATIONS = int(
    os.environ.get("MENTOR_MAX_DAILY_NOTIFICATIONS", "36")
)
LIVE_KICKOFF_ENABLED = os.environ.get("LIVE_KICKOFF_ENABLED", "1") == "1"
LIVE_KICKOFF_DELAY_SECONDS = int(os.environ.get("LIVE_KICKOFF_DELAY_SECONDS", "12"))


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
