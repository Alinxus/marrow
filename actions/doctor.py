"""Self-healing helpers for /doctor diagnostics."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import config
from storage import db


def _env_value_present(key: str) -> bool:
    return bool(str(os.environ.get(key, "")).strip())


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _run_pip_install(packages: list[str]) -> str:
    if not packages:
        return "no packages requested"
    cmd = [sys.executable, "-m", "pip", "install", *packages]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if proc.returncode == 0:
            return f"installed: {', '.join(packages)}"
        tail = (proc.stderr or proc.stdout or "").strip()[-240:]
        return f"failed: {', '.join(packages)} ({tail})"
    except Exception as exc:
        return f"failed: {', '.join(packages)} ({exc})"


def _ensure_env_file() -> tuple[str, bool]:
    env_file = Path(getattr(config, "ENV_FILE", Path.home() / ".marrow" / ".env"))
    env_file.parent.mkdir(parents=True, exist_ok=True)
    if env_file.exists():
        return (f"env exists: {env_file}", False)

    repo_root = Path(__file__).resolve().parents[1]
    template = repo_root / ".env.example"
    if template.exists():
        shutil.copy2(template, env_file)
        return (f"created env from template: {env_file}", True)

    env_file.write_text("OPENAI_API_KEY=\n", encoding="utf-8")
    return (f"created minimal env: {env_file}", True)


def _ensure_env_defaults() -> list[str]:
    env_file = Path(getattr(config, "ENV_FILE", Path.home() / ".marrow" / ".env"))
    if not env_file.exists():
        return []

    required_defaults = {
        "WEB_UI_ENABLED": "1",
        "MARROW_APPROVAL_MODE": "guarded",
        "CONVERSATION_ENABLED": "1",
    }
    text = env_file.read_text(encoding="utf-8", errors="ignore")
    changes = []
    for k, v in required_defaults.items():
        if f"{k}=" not in text:
            text += f"\n{k}={v}"
            changes.append(f"added {k}={v}")
    if changes:
        env_file.write_text(text.strip() + "\n", encoding="utf-8")
    return changes


def apply_auto_fixes() -> str:
    """Attempt safe auto-fixes and return a human-readable report."""
    lines = ["## Doctor Auto-Fix"]

    msg, created = _ensure_env_file()
    lines.append(f"- {msg}")

    default_changes = _ensure_env_defaults()
    if default_changes:
        for c in default_changes:
            lines.append(f"- {c}")
    else:
        lines.append("- env defaults already present")

    installs: list[list[str]] = []

    if not _has_module("httpx"):
        installs.append(["httpx"])
    if not _has_module("numpy"):
        installs.append(["numpy"])
    if not _has_module("sounddevice"):
        installs.append(["sounddevice"])
    if not _has_module("websockets"):
        installs.append(["websockets"])

    if os.name == "nt":
        if not _has_module("PySide6"):
            installs.append(["PySide6"])
        # QtWebEngine module path for PySide6
        if not _has_module("PySide6.QtWebEngineWidgets"):
            installs.append(["PySide6-WebEngine"])

    if getattr(config, "DEEPGRAM_API_KEY", "") and not _has_module("deepgram"):
        installs.append(["deepgram-sdk"])

    if installs:
        for pkg_group in installs:
            lines.append(f"- pip { _run_pip_install(pkg_group) }")
    else:
        lines.append("- python deps look present")

    try:
        from actions.permissions import check_permissions, open_permission_panels

        report = check_permissions(detailed=False)
        if "MISSING" in report or "issue(s) detected" in report.lower():
            lines.append(f"- permissions: {open_permission_panels()}")
        else:
            lines.append("- permissions look healthy")
    except Exception as exc:
        lines.append(f"- permissions check failed: {exc}")

    lines.append("- restart Marrow after auto-fix so env/deps reload")
    return "\n".join(lines)


def diagnose_failures(deep: bool = False) -> str:
    """Root-cause analysis for 'it says done but does nothing' type failures."""
    from actions.capabilities import runtime_capability_snapshot
    from actions.permissions import check_permissions

    lines = ["## Doctor Failure Analysis"]
    causes: list[str] = []
    fixes: list[str] = []

    snap = runtime_capability_snapshot()
    providers = snap.get("providers", {})
    audio = snap.get("audio", {})

    if not any(bool(v) for v in providers.values()):
        causes.append("No LLM provider key configured.")
        fixes.append("Set OPENAI_API_KEY (or ANTHROPIC_API_KEY) in ~/.marrow/.env and restart.")

    if os.environ.get("AUDIO_ENABLED", "1") == "1":
        if not audio.get("deepgram") and not audio.get("whisper_backend"):
            causes.append("Audio enabled but no usable STT backend detected.")
            fixes.append("Install faster-whisper or set DEEPGRAM_API_KEY + deepgram-sdk.")

    if _env_value_present("DEEPGRAM_API_KEY") and not _has_module("deepgram"):
        causes.append("DEEPGRAM_API_KEY is set but deepgram-sdk is not installed.")
        fixes.append("Run: python -m pip install deepgram-sdk")

    if _has_module("kokoro_onnx"):
        voices = Path.home() / ".cache" / "kokoro-onnx" / "voices-v1.0.bin"
        if not voices.exists() and not _env_value_present("ELEVENLABS_API_KEY") and not _env_value_present("DEEPGRAM_API_KEY"):
            causes.append("Kokoro installed but voices file missing for local TTS fallback.")
            fixes.append(f"Download voices-v1.0.bin to {voices}")

    try:
        perm_report = check_permissions(detailed=False)
        if "MISSING" in perm_report or "issue(s) detected" in perm_report.lower():
            causes.append("OS permissions/capabilities are degraded.")
            fixes.append("Run open_permission_panels and grant required permissions, then restart Marrow.")
    except Exception:
        pass

    try:
        runtime = db.get_runtime_snapshot() or {}
        ac = runtime.get("audio_capture") or {}
        if ac.get("status") in {"paused", "error", "unavailable"}:
            causes.append(f"audio_capture is {ac.get('status')} ({(ac.get('detail') or '')[:90]}).")
            fixes.append("Set AUDIO_INPUT_DEVICE to a valid microphone index or set AUDIO_ENABLED=0.")
    except Exception:
        pass

    approval_mode = os.environ.get("MARROW_APPROVAL_MODE", "guarded").strip().lower() or "guarded"
    if approval_mode != "unlocked":
        causes.append("Approval mode is guarded; dangerous actions can be blocked.")
        fixes.append("If you want full autonomy: set MARROW_APPROVAL_MODE=unlocked.")

    try:
        from brain.agi import get_agi

        agi_stats = get_agi().get_stats()
        retry_q = int(agi_stats.get("ingest_retry_queue", 0))
        if retry_q > 0:
            causes.append(f"AGI ingest retry queue has {retry_q} pending item(s).")
            fixes.append("Check network/API key for memory backend; retries will flush automatically.")
    except Exception:
        pass

    if not causes:
        lines.append("- No obvious hard failure found. Runtime looks healthy.")
        lines.append("- If tasks still 'say done' without effect, run explicit commands (e.g., open file explorer) and inspect tool outputs.")
    else:
        lines.append("### Likely Causes")
        for c in causes[:12]:
            lines.append(f"- {c}")
        lines.append("")
        lines.append("### Recommended Fixes")
        seen = set()
        for f in fixes:
            if f in seen:
                continue
            seen.add(f)
            lines.append(f"- {f}")

    if deep:
        lines.append("")
        lines.append("### Deep Runtime Snapshot")
        lines.append(f"- Python: {sys.executable}")
        lines.append(f"- Env file: {getattr(config, 'ENV_FILE', '')}")
        lines.append(f"- Provider flags: {providers}")
        lines.append(f"- Audio flags: {audio}")
        try:
            age = db.get_last_screenshot_age_seconds()
            lines.append(f"- Last screenshot age: {int(age) if age is not None else 'none'}")
        except Exception as exc:
            lines.append(f"- Last screenshot age: unavailable ({exc})")
        try:
            runtime = db.get_runtime_snapshot() or {}
            lines.append("- Runtime components:")
            for name in sorted(runtime.keys()):
                comp = runtime[name]
                lines.append(
                    f"  - {name}: {comp.get('status')} ({int(comp.get('age_seconds', 0))}s) {(comp.get('detail') or '')[:90]}"
                )
        except Exception as exc:
            lines.append(f"- Runtime components unavailable ({exc})")

    return "\n".join(lines)
