"""Dynamic runtime capability discovery for Marrow."""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import sys
from pathlib import Path

import config


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def runtime_capability_snapshot() -> dict:
    env_file = str(getattr(config, "ENV_FILE", Path.home() / ".marrow" / ".env"))
    cwd = Path.cwd().resolve()
    repo_venv = cwd / ".venv" / "Scripts" / "python.exe"
    return {
        "platform": platform.system(),
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "repo_venv_python": str(repo_venv) if repo_venv.exists() else "",
        "env_file": env_file,
        "commands": {
            name: bool(shutil.which(name))
            for name in ("git", "python", "node", "npm", "ollama", "gh", "bash", "pwsh")
        },
        "providers": {
            "openai": bool(config.OPENAI_API_KEY),
            "anthropic": bool(config.ANTHROPIC_API_KEY),
            "ollama": bool(config.OLLAMA_BASE_URL),
        },
        "audio": {
            "enabled": bool(config.AUDIO_ENABLED),
            "deepgram": bool(config.DEEPGRAM_API_KEY),
            "whisper_backend": bool(_has_module("faster_whisper")),
            "elevenlabs": bool(config.ELEVENLABS_API_KEY),
            "kokoro": bool(_has_module("kokoro_onnx")),
        },
        "browser": {
            "browser_use": _has_module("browser_use"),
            "pyautogui": _has_module("pyautogui"),
        },
        "memory": {
            "retaindb": bool(config.RETAINDB_API_KEY),
            "db_path": str(Path.home() / ".marrow" / "marrow.db"),
        },
        "smart_home": {
            "home_assistant": bool(config.HOME_ASSISTANT_URL and config.HOME_ASSISTANT_TOKEN),
        },
    }


def capability_summary_text() -> str:
    snap = runtime_capability_snapshot()
    lines = [
        f"Platform: {snap['platform']} Python {snap['python']}",
        f"Interpreter: {snap['python_executable']}",
        "Commands: " + ", ".join(
            f"{name}={'yes' if ok else 'no'}" for name, ok in snap["commands"].items()
        ),
        "Providers: " + ", ".join(
            f"{name}={'ready' if ok else 'off'}" for name, ok in snap["providers"].items()
        ),
        "Audio: " + ", ".join(
            f"{name}={'yes' if ok else 'no'}" for name, ok in snap["audio"].items()
        ),
        "Browser: " + ", ".join(
            f"{name}={'yes' if ok else 'no'}" for name, ok in snap["browser"].items()
        ),
        "Smart home: " + ("ready" if snap["smart_home"]["home_assistant"] else "off"),
    ]
    return "\n".join(lines)


def capability_summary_json() -> str:
    return json.dumps(runtime_capability_snapshot(), indent=2)
