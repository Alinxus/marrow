"""
Marrow — ambient intelligence.

Threading model:
  Main thread  → PyQt6 event loop (UI MUST be on main thread on Windows)
  Daemon thread → asyncio event loop (all async work: capture, reasoning, TTS)

Communication:
  asyncio → Qt : emit Qt signals via bridge (thread-safe by Qt design)
  Qt → asyncio : run_coroutine_threadsafe(coro, _asyncio_loop)

UI architecture:
  MarrowControlBar — compact floating bar with hover expansion and chat
  ToastManager    — slide-in text notifications (substitute for voice)
  ApprovalDialog  — dangerous-action confirmation popup
  SettingsPanel   — settings editor
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

# Keep BLAS/OpenMP footprint tiny on constrained Windows machines.
# Must be set before importing modules that may load NumPy/OpenBLAS.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

import config
from storage import db, state_store
from capture.screen import screen_capture_loop
from capture.audio import AudioCaptureService, set_wake_word_callback
from capture.audio import set_conversation_turn_callback
from brain.reasoning import (
    reasoning_loop,
    _run_reasoning,
    _handle_result,
    _build_context_summary,
    _build_deep_world_context,
    _build_semantic_memory_context,
)
from brain.interrupt import InterruptDecisionEngine
from on_demand import (
    init_on_demand,
    set_activation_callback,
    set_main_loop as od_set_loop,
)
from actions.approval import set_confirm_callback
from actions.scheduler import init_scheduler, shutdown_scheduler


# ─── Logging ──────────────────────────────────────────────────────────────────


def _setup_logging() -> None:
    log_dir = Path.home() / ".marrow"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / "marrow.log", encoding="utf-8"),
        ],
    )


log = logging.getLogger("marrow")

_RUNTIME_HANDOFF_ENV = "MARROW_RUNTIME_HANDOFF"


def _python_runtime_warning() -> str:
    try:
        import sys

        repo_python = (Path.cwd() / ".venv" / "Scripts" / "python.exe").resolve()
        current = Path(sys.executable).resolve()
        if repo_python.exists() and current != repo_python:
            return (
                f"Marrow is running with {current} instead of repo venv {repo_python}. "
                "Capabilities may look wrong until you launch from the project venv."
            )
    except Exception:
        pass
    return ""


def _ensure_best_runtime() -> bool:
    """Re-exec into the repo venv when available so capabilities match shipping runtime."""
    try:
        if os.environ.get(_RUNTIME_HANDOFF_ENV) == "1":
            return False
        repo_python = (Path.cwd() / ".venv" / "Scripts" / "python.exe").resolve()
        current = Path(sys.executable).resolve()
        if repo_python.exists() and current != repo_python:
            env = os.environ.copy()
            env[_RUNTIME_HANDOFF_ENV] = "1"
            args = [str(repo_python), str(Path(__file__).resolve())]
            subprocess.Popen(args, cwd=str(Path.cwd()), env=env)
            return True
    except Exception:
        pass
    return False


def _detect_ollama_models() -> list[str]:
    env = os.environ.copy()
    env["PATH"] = env.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"
    for exe in ("ollama", "/opt/homebrew/bin/ollama", "/usr/local/bin/ollama"):
        try:
            p = subprocess.run(
                [exe, "list"],
                capture_output=True,
                text=True,
                timeout=8,
                env=env,
            )
            if p.returncode != 0:
                continue
            out = []
            for line in (p.stdout or "").splitlines():
                s = line.strip()
                if not s or s.lower().startswith("name"):
                    continue
                name = s.split()[0].strip()
                if name and name not in out:
                    out.append(name)
            if out:
                return out
        except Exception:
            pass
    return []


def _apply_settings_updates(updates: dict[str, str]) -> str:
    """Persist settings to env file + apply to running process."""
    from dotenv import dotenv_values
    import importlib

    env_path = Path(getattr(config, "ENV_FILE", Path.home() / ".marrow" / ".env"))
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing = dict(dotenv_values(env_path)) if env_path.exists() else {}

    for k, v in updates.items():
        existing[k] = str(v)
        os.environ[k] = str(v)

    lines = [f'{k}="{str(v).replace('"', '\\"')}"\n' for k, v in existing.items()]
    env_path.write_text("".join(lines), encoding="utf-8")

    # Hot apply runtime config + llm client
    if "config" in sys.modules:
        importlib.reload(sys.modules["config"])
    from brain.llm import reset_client

    reset_client()
    return f"Updated settings: {', '.join(updates.keys())}"


def _enforce_default_behavior_profile() -> None:
    """Make talkative proactive + conversation mode the startup default."""
    desired = {
        "CONVERSATION_ENABLED": "1",
        "CONVERSATION_RESPONSE_STYLE": "detailed",
        "CONVERSATION_MODEL_TYPE": "reasoning",
        "CONVERSATION_MODE_TIMEOUT_SECONDS": "120",
        "CONVERSATION_MAX_TURNS": "20",
        "CONVERSATION_MAX_TOKENS": "420",
        "PROACTIVE_FREQUENCY": "4",
        "PROACTIVE_SPEECH_MIN_URGENCY": "2",
        "PROACTIVE_AUTO_SPEAK_MIN_URGENCY": "2",
        "PROACTIVE_SPEECH_MIN_GAP_SECONDS": "30",
        "PROACTIVE_SIGNAL_DEDUP_SECONDS": "180",
    }
    pending = {
        k: v for k, v in desired.items() if str(os.environ.get(k, "")).strip() != v
    }
    if not pending:
        return
    try:
        _apply_settings_updates(pending)
        log.info("Applied default talkative + conversation behavior profile")
    except Exception as exc:
        log.warning(f"Failed applying default behavior profile: {exc}")


def _handle_slash_command(text: str) -> str | None:
    """Handle lightweight slash commands from chat.

    Returns response string if handled, otherwise None.
    """
    t = (text or "").strip()
    if not t.startswith("/"):
        return None

    parts = t.split()
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in ("/help", "/commands"):
        return (
            "Slash commands:\n"
            "- /models\n"
            "- /provider <auto|openai|anthropic|ollama|none>\n"
            "- /model <reasoning|scoring|vision> <model_name>\n"
            "- /capabilities\n"
            "- /selfcheck\n"
            "- /doctor\n"
            "- /chatstyle <short|balanced|detailed|status>\n"
            "- /proactive <quiet|normal|talkative|status>\n"
            "- /conversation <on|off|status>\n"
            "- /mission <start|pause|resume|rollback|status> [goal]\n"
            "- /swarm <run|status> [goal]\n"
            "- /audio <on|off|status>\n"
            "- /hotkey <on|off>\n"
            "- /wakeword <on|off>"
        )

    if cmd == "/models":
        llm_provider = os.environ.get("LLM_PROVIDER", config.LLM_PROVIDER)
        lines = [
            f"Provider: {llm_provider}",
            f"Anthropic: reasoning={os.environ.get('REASONING_MODEL', config.REASONING_MODEL)}, scoring={os.environ.get('SCORING_MODEL', config.SCORING_MODEL)}, vision={os.environ.get('VISION_MODEL', config.VISION_MODEL)}",
            f"OpenAI: reasoning={os.environ.get('OPENAI_REASONING_MODEL', config.OPENAI_REASONING_MODEL)}, scoring={os.environ.get('OPENAI_SCORING_MODEL', config.OPENAI_SCORING_MODEL)}, vision={os.environ.get('OPENAI_VISION_MODEL', config.OPENAI_VISION_MODEL)}",
            f"Ollama: base={os.environ.get('OLLAMA_BASE_URL', config.OLLAMA_BASE_URL)}, reasoning={os.environ.get('OLLAMA_REASONING_MODEL', config.OLLAMA_REASONING_MODEL)}, scoring={os.environ.get('OLLAMA_SCORING_MODEL', config.OLLAMA_SCORING_MODEL)}, vision={os.environ.get('OLLAMA_VISION_MODEL', config.OLLAMA_VISION_MODEL)}",
        ]
        ollama_models = _detect_ollama_models()
        if ollama_models:
            lines.append("Installed Ollama models: " + ", ".join(ollama_models[:12]))
        else:
            lines.append("Installed Ollama models: none detected (is ollama running?)")
        return "\n".join(lines)

    if cmd == "/provider":
        if not args:
            return "Usage: /provider <auto|openai|anthropic|ollama|none>"
        provider = args[0].lower()
        if provider not in ("auto", "openai", "anthropic", "ollama", "none"):
            return "Invalid provider. Use: auto|openai|anthropic|ollama|none"
        return _apply_settings_updates({"LLM_PROVIDER": provider})

    if cmd == "/model":
        if len(args) < 2:
            return "Usage: /model <reasoning|scoring|vision> <model_name>"
        kind = args[0].lower()
        name = " ".join(args[1:]).strip()
        if kind not in ("reasoning", "scoring", "vision"):
            return "First arg must be reasoning|scoring|vision"
        provider = os.environ.get("LLM_PROVIDER", config.LLM_PROVIDER).lower()
        updates = {}
        if provider == "openai":
            key = {
                "reasoning": "OPENAI_REASONING_MODEL",
                "scoring": "OPENAI_SCORING_MODEL",
                "vision": "OPENAI_VISION_MODEL",
            }[kind]
            updates[key] = name
        elif provider == "ollama":
            key = {
                "reasoning": "OLLAMA_REASONING_MODEL",
                "scoring": "OLLAMA_SCORING_MODEL",
                "vision": "OLLAMA_VISION_MODEL",
            }[kind]
            updates[key] = name
        else:
            key = {
                "reasoning": "REASONING_MODEL",
                "scoring": "SCORING_MODEL",
                "vision": "VISION_MODEL",
            }[kind]
            updates[key] = name
        return _apply_settings_updates(updates)

    if cmd == "/capabilities":
        from actions.capabilities import capability_summary_text

        return capability_summary_text()

    if cmd == "/selfcheck":
        lines = []
        runtime_warning = _python_runtime_warning()
        if runtime_warning:
            lines.append("Runtime:")
            lines.append(runtime_warning)
        from actions.capabilities import capability_summary_text

        lines.append(capability_summary_text())
        return "\n".join(lines)

    if cmd == "/doctor":
        from actions.capabilities import capability_summary_text
        from actions.permissions import check_permissions
        from brain.llm import get_client

        lines = ["## Marrow Doctor"]
        runtime_warning = _python_runtime_warning()
        if runtime_warning:
            lines.append(f"- Runtime warning: {runtime_warning}")

        try:
            llm = get_client()
            lines.append(
                f"- LLM runtime: provider={llm.provider}, reasoning_model={llm.model_for('reasoning')}"
            )
        except Exception as e:
            lines.append(f"- LLM runtime: unavailable ({e})")

        lines.append("")
        lines.append(capability_summary_text())
        lines.append("")
        lines.append(check_permissions(detailed=True))
        lines.append("")
        lines.append(
            "Recommended fixes: if anything is missing, run open_permission_panels, then restart terminal + Marrow."
        )
        return "\n".join(lines)

    if cmd == "/chatstyle":
        if not args or args[0].lower() == "status":
            current = os.environ.get(
                "CONVERSATION_RESPONSE_STYLE", config.CONVERSATION_RESPONSE_STYLE
            )
            return f"Chat style: {current}"
        style = args[0].lower()
        if style not in ("short", "balanced", "detailed"):
            return "Usage: /chatstyle <short|balanced|detailed|status>"
        return _apply_settings_updates({"CONVERSATION_RESPONSE_STYLE": style})

    if cmd == "/proactive":
        if not args or args[0].lower() == "status":
            min_u = os.environ.get(
                "PROACTIVE_SPEECH_MIN_URGENCY", str(config.PROACTIVE_SPEECH_MIN_URGENCY)
            )
            auto_min = os.environ.get(
                "PROACTIVE_AUTO_SPEAK_MIN_URGENCY",
                str(config.PROACTIVE_AUTO_SPEAK_MIN_URGENCY),
            )
            gap = os.environ.get(
                "PROACTIVE_SPEECH_MIN_GAP_SECONDS",
                str(config.PROACTIVE_SPEECH_MIN_GAP_SECONDS),
            )
            dedup = os.environ.get(
                "PROACTIVE_SIGNAL_DEDUP_SECONDS",
                str(config.PROACTIVE_SIGNAL_DEDUP_SECONDS),
            )
            return f"Proactive: min_urgency={min_u}, auto_speak_min={auto_min}, speech_gap={gap}s, dedup={dedup}s"
        mode = args[0].lower()
        profiles = {
            "quiet": {
                "PROACTIVE_SPEECH_MIN_URGENCY": "4",
                "PROACTIVE_AUTO_SPEAK_MIN_URGENCY": "4",
                "PROACTIVE_SPEECH_MIN_GAP_SECONDS": "120",
                "PROACTIVE_SIGNAL_DEDUP_SECONDS": "600",
            },
            "normal": {
                "PROACTIVE_SPEECH_MIN_URGENCY": "3",
                "PROACTIVE_AUTO_SPEAK_MIN_URGENCY": "3",
                "PROACTIVE_SPEECH_MIN_GAP_SECONDS": "60",
                "PROACTIVE_SIGNAL_DEDUP_SECONDS": "420",
            },
            "talkative": {
                "PROACTIVE_SPEECH_MIN_URGENCY": "2",
                "PROACTIVE_AUTO_SPEAK_MIN_URGENCY": "2",
                "PROACTIVE_SPEECH_MIN_GAP_SECONDS": "30",
                "PROACTIVE_SIGNAL_DEDUP_SECONDS": "180",
            },
        }
        if mode not in profiles:
            return "Usage: /proactive <quiet|normal|talkative|status>"
        return _apply_settings_updates(profiles[mode])

    if cmd == "/audio" and args and args[0].lower() == "status":
        from actions.capabilities import capability_summary_text

        audio_lines = [
            line
            for line in capability_summary_text().splitlines()
            if line.startswith("Audio:")
        ]
        return "\n".join(audio_lines) if audio_lines else "Audio status unavailable."

    if cmd in ("/audio", "/hotkey", "/wakeword"):
        if not args or args[0].lower() not in ("on", "off"):
            return (
                f"Usage: {cmd} <on|off>"
                if cmd != "/audio"
                else "Usage: /audio <on|off|status>"
            )
        val = "1" if args[0].lower() == "on" else "0"
        key = {
            "/audio": "AUDIO_ENABLED",
            "/hotkey": "HOTKEY_ENABLED",
            "/wakeword": "WAKE_WORD_ENABLED",
        }[cmd]
        return _apply_settings_updates({key: val})

    if cmd == "/conversation":
        from brain import conversation

        if not args:
            return "Usage: /conversation <on|off|status>"
        mode = args[0].lower()
        if mode == "status":
            active = conversation.is_active()
            enabled = os.environ.get("CONVERSATION_ENABLED", "1") == "1"
            return (
                f"Conversation: {'enabled' if enabled else 'disabled'}, {'active' if active else 'inactive'}"
                + (f" ({conversation.remaining_seconds()}s left)" if active else "")
            )
        if mode == "on":
            _apply_settings_updates({"CONVERSATION_ENABLED": "1"})
            conversation.activate_session()
            return f"Conversation mode ON ({conversation.remaining_seconds()}s)."
        if mode == "off":
            _apply_settings_updates({"CONVERSATION_ENABLED": "0"})
            conversation.end_session()
            return "Conversation mode OFF."
        return "Usage: /conversation <on|off|status>"

    return f"Unknown command: {cmd}. Try /help"


async def _handle_mission_command(text: str) -> str | None:
    t = (text or "").strip()
    if not t.startswith("/mission"):
        return None

    parts = t.split(maxsplit=2)
    action = parts[1].lower() if len(parts) > 1 else "status"
    goal = parts[2].strip() if len(parts) > 2 else ""

    from brain.mission import get_mission_controller

    controller = get_mission_controller()

    if action in ("start", "run"):
        if not goal:
            return "Usage: /mission start <goal>"
        mission = await controller.start_mission(goal)
        return (
            f"Started mission {mission.mission_id[:8]} with {len(mission.steps)} steps. "
            f"State: {mission.state}."
        )
    if action == "pause":
        return await controller.pause_current()
    if action == "resume":
        return await controller.resume_current()
    if action == "rollback":
        return await controller.rollback_current()
    if action == "status":
        return controller.current_status_text()
    return "Usage: /mission <start|pause|resume|rollback|status> [goal]"


async def _handle_swarm_command(text: str) -> str | None:
    t = (text or "").strip()
    if not t.startswith("/swarm"):
        return None

    parts = t.split(maxsplit=2)
    action = parts[1].lower() if len(parts) > 1 else "status"
    goal = parts[2].strip() if len(parts) > 2 else ""

    from brain.swarm import get_swarm_coordinator

    coordinator = get_swarm_coordinator()
    if action == "run":
        if not goal:
            return "Usage: /swarm run <goal>"
        return await coordinator.run(goal)
    if action == "status":
        return coordinator.status_text()
    return "Usage: /swarm <run|status> [goal]"


# ─── Global asyncio loop reference ────────────────────────────────────────────

_asyncio_loop: Optional[asyncio.AbstractEventLoop] = None
_shutdown_event: Optional[asyncio.Event] = None


def _request_shutdown(*_) -> None:
    """Thread-safe shutdown from any context."""
    if _asyncio_loop and _shutdown_event:
        _asyncio_loop.call_soon_threadsafe(_shutdown_event.set)
    else:
        sys.exit(0)


# ─── Asyncio backend ───────────────────────────────────────────────────────────


async def _main_async() -> None:
    global _shutdown_event

    _shutdown_event = asyncio.Event()

    log.info(f"Starting {config.MARROW_NAME} backend…")

    db.init_db()
    db.prune_old_data(days=7)
    state_store.init_state_store()
    init_scheduler()
    _enforce_default_behavior_profile()

    audio_service = AudioCaptureService()
    interrupt_engine = InterruptDecisionEngine()

    # ── Bridge ────────────────────────────────────────────────────────────
    try:
        from ui.bridge import get_bridge

        _bridge = get_bridge()
    except Exception:
        _bridge = None

    def _emit(signal_name: str, *args):
        if _bridge:
            try:
                getattr(_bridge, signal_name).emit(*args)
            except Exception:
                pass

    # ── Approval wiring ───────────────────────────────────────────────────
    async def _request_approval_async(request) -> bool:
        if _bridge:
            return await _bridge.request_approval(
                request.description, request.command[:120]
            )
        log.warning(f"[APPROVAL REQUIRED — no UI] {request.description}: blocked")
        return False

    def _approval_callback(request):
        if _asyncio_loop:
            future = asyncio.run_coroutine_threadsafe(
                _request_approval_async(request), _asyncio_loop
            )
            try:
                return future.result(timeout=35)
            except Exception:
                return False
        return False

    set_confirm_callback(_approval_callback)

    # ── On-demand activation ──────────────────────────────────────────────
    def _looks_like_action_request(text: str) -> bool:
        low = (text or "").strip().lower()
        if not low:
            return False
        if low.startswith(("what", "why", "how", "when", "who", "where")):
            return False
        action_prefixes = (
            "open ",
            "close ",
            "start ",
            "stop ",
            "run ",
            "create ",
            "write ",
            "search ",
            "find ",
            "check ",
            "send ",
            "schedule ",
            "remind ",
            "call ",
            "email ",
            "message ",
            "summarize ",
        )
        if low.startswith(action_prefixes):
            return True
        if low.startswith(("can you ", "could you ", "please ", "go ahead and ")):
            return True
        return False

    async def _handle_conversation_turn(user_text: str) -> None:
        """Low-latency conversational turn handler (voice-first back-and-forth)."""
        if not config.CONVERSATION_ENABLED:
            return

        from brain import conversation
        from actions.executor import execute_action
        from voice.speak import speak

        _emit("state_changed", "thinking")
        try:
            ctx = db.get_recent_context(30)
            hint = _build_context_summary(ctx)
            if _looks_like_action_request(user_text):
                action_result = await execute_action(user_text, context=hint)
                action_text = (action_result or "").strip()
                if action_text:
                    reply = await conversation.handle_turn(
                        f"I executed the user's request. Result: {action_text[:500]}",
                        context_hint=hint,
                    )
                else:
                    reply = "Done."
            else:
                reply = await conversation.handle_turn(user_text, context_hint=hint)
            if reply:
                _emit("message_spoken", reply, 4)
                await speak(reply)
        except Exception as e:
            log.error(f"Conversation turn error: {e}")
            _emit("state_changed", "error")
        finally:
            _emit("state_changed", "idle")

    async def on_activation(reason: str) -> None:
        log.info(f"On-demand activation: {reason}")
        from brain import conversation
        from voice.speak import speak_filler

        if not config.CONVERSATION_ENABLED:
            _emit("state_changed", "thinking")
            try:
                context = db.get_recent_context(config.CONTEXT_WINDOW_SECONDS)
                ctx_str = _build_context_summary(context)
                deep_world = _build_deep_world_context()
                memory_ctx = await _build_semantic_memory_context(ctx_str)
                full_ctx = "\n\n".join(filter(None, [deep_world, memory_ctx, ctx_str]))
                result = await _run_reasoning(full_ctx)
                if result:
                    await _handle_result(result, ctx_str, interrupt_engine)
            except Exception as e:
                log.error(f"On-demand activation error: {e}")
                _emit("state_changed", "error")
            else:
                _emit("state_changed", "idle")
            return

        # Enter conversational mode on wake/hotkey activation.
        conversation.activate_session()

        # If wake phrase carried a query, answer via fast convo path.
        query = conversation.extract_wake_query(str(reason or ""))
        if query and len(query.strip()) > 1:
            await _handle_conversation_turn(query)
            return

        # If no query, acknowledge quickly and wait for follow-up.
        try:
            await speak_filler()
        except Exception:
            pass
        _emit("state_changed", "idle")
        return

    od_set_loop(_asyncio_loop)
    set_activation_callback(on_activation)
    set_wake_word_callback(on_activation)
    set_conversation_turn_callback(_handle_conversation_turn)
    audio_service.set_loop(_asyncio_loop)
    if _bridge:
        _bridge.set_loop(_asyncio_loop)
        _bridge.set_activation_callback(on_activation)
    init_on_demand()

    if config.MISSION_RECOVERY_ENABLED:
        try:
            from brain.mission import get_mission_controller

            recovered = get_mission_controller().recover_last_mission()
            if recovered:
                _emit(
                    "toast_requested",
                    config.MARROW_NAME,
                    f"Recovered paused mission: {recovered.goal[:120]}",
                    3,
                )
        except Exception as exc:
            log.warning(f"Mission recovery skipped: {exc}")

    runtime_warning = _python_runtime_warning()
    if runtime_warning:
        _emit("toast_requested", config.MARROW_NAME, runtime_warning[:220], 2)

    # ── Startup permission health check ───────────────────────────────────
    try:
        from actions import permissions as _perms

        perm_report = _perms.check_permissions(detailed=False)
        low = perm_report.lower()
        if "missing" in low or "issue(s) detected" in low:
            _emit(
                "toast_requested",
                config.MARROW_NAME,
                "Permission/capability issues detected. Run 'check_permissions' for details.",
                2,
            )
        else:
            _emit(
                "toast_requested",
                config.MARROW_NAME,
                "Permissions look healthy.",
                5,
            )
    except Exception:
        pass

    # ── Patch speak() → bridge signals + toast ────────────────────────────
    try:
        import voice.speak as _speak_mod

        _orig_speak = _speak_mod.speak

        async def _patched_speak(text: str) -> None:
            _emit("state_changed", "speaking")
            await _orig_speak(text)
            _emit("state_changed", "idle")

        _speak_mod.speak = _patched_speak
    except Exception:
        pass

    # ── Patch interrupt engine → bridge message_spoken ────────────────────
    try:
        _orig_record = interrupt_engine.record_spoken

        def _patched_record(candidate):
            _orig_record(candidate)
            _emit("message_spoken", candidate.message, candidate.urgency)

        interrupt_engine.record_spoken = _patched_record
    except Exception:
        pass

    # ── Periodic stats ────────────────────────────────────────────────────
    async def _stats_loop() -> None:
        import json

        while not _shutdown_event.is_set():
            try:
                ctx = db.get_recent_context(3600)
                stats = {
                    "screenshots": len(ctx.get("screenshots", [])),
                    "speaks": len(db.get_recent_actions(limit=200)),
                    "actions": sum(
                        1 for a in db.get_recent_actions(limit=200) if a.get("success")
                    ),
                }
                _emit("stats_updated", json.dumps(stats))
            except Exception:
                pass
            await asyncio.sleep(30)

    # ── Log startup info ──────────────────────────────────────────────────
    try:
        from brain.llm import get_client

        _llm = get_client()
        log.info(f"  Provider  : {_llm.provider} (configured={config.LLM_PROVIDER})")
        log.info(f"  Reasoning : {_llm.model_for('reasoning')}")
    except Exception:
        log.info(f"  Provider  : {config.LLM_PROVIDER}")
        log.info("  Reasoning : unknown")
    log.info(f"  Whisper   : {config.WHISPER_MODEL}")
    log.info(f"  Voice     : {'ElevenLabs' if config.VOICE_ENABLED else 'SAPI/off'}")
    log.info(
        f"  Hotkey    : {config.ON_DEMAND_HOTKEY if config.HOTKEY_ENABLED else 'off'}"
    )
    log.info("Backend running.")

    # ── Signals ───────────────────────────────────────────────────────────
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _request_shutdown)
        except (OSError, ValueError):
            pass

    # ── Supervised task runner ────────────────────────────────────────────
    async def _supervised(name: str, coro_factory, *args) -> None:
        while not _shutdown_event.is_set():
            try:
                log.info(f"Starting {name}")
                await coro_factory(*args)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"{name} crashed: {e} — restarting in 5s", exc_info=True)
                await asyncio.sleep(5)
        log.info(f"{name} stopped.")

    # ── Proactive loop ────────────────────────────────────────────────────
    async def _proactive_loop():
        try:
            from brain.proactive import proactive_loop

            await proactive_loop()
        except Exception as e:
            log.warning(f"Proactive loop unavailable: {e}")
            while not _shutdown_event.is_set():
                await asyncio.sleep(60)

    async def _predictive_loop():
        try:
            from brain.predictive import predictive_loop

            await predictive_loop()
        except Exception as e:
            log.warning(f"Predictive loop unavailable: {e}")
            while not _shutdown_event.is_set():
                await asyncio.sleep(60)

    # ── Claim verifier loop ───────────────────────────────────────────────
    async def _claim_verifier_loop():
        try:
            from brain.claim_verifier import claim_verification_loop

            await claim_verification_loop()
        except Exception as e:
            log.warning(f"Claim verifier unavailable: {e}")
            while not _shutdown_event.is_set():
                await asyncio.sleep(60)

    # ── Wiki: load on startup ─────────────────────────────────────────────
    try:
        from brain.wiki import get_wiki, wiki_update_loop

        get_wiki()  # loads from disk
    except Exception as e:
        log.warning(f"Wiki init failed: {e}")

    # ── AGI: init on startup ──────────────────────────────────────────────
    try:
        from brain.agi import get_agi, agi_loop

        get_agi()  # init singleton
    except Exception as e:
        log.warning(f"AGI init failed: {e}")

    # ── Launch all tasks ──────────────────────────────────────────────────
    tasks = [
        asyncio.create_task(_supervised("screen_capture", screen_capture_loop)),
        asyncio.create_task(_supervised("reasoning", reasoning_loop, interrupt_engine)),
        asyncio.create_task(_supervised("wiki_update", wiki_update_loop)),
        asyncio.create_task(_supervised("agi", agi_loop)),
        asyncio.create_task(_supervised("claim_verifier", _claim_verifier_loop)),
        asyncio.create_task(_supervised("proactive", _proactive_loop)),
        asyncio.create_task(_supervised("predictive", _predictive_loop)),
        asyncio.create_task(_stats_loop()),
        asyncio.create_task(_shutdown_event.wait()),
    ]

    if config.AUDIO_ENABLED:
        tasks.append(
            asyncio.create_task(_supervised("audio_capture", audio_service.run))
        )
    else:
        log.info("Audio capture disabled (AUDIO_ENABLED=0)")

    # Startup welcome — fires 15s in, once per day.
    # NOT added to the monitored tasks list — it completes by design and must
    # not trigger the FIRST_COMPLETED shutdown.
    try:
        from brain.startup import run_startup_sequence

        asyncio.create_task(run_startup_sequence(interrupt_engine))
    except Exception as e:
        log.warning(f"Startup sequence init failed: {e}")

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    if config.AUDIO_ENABLED:
        audio_service.stop()
    shutdown_scheduler()
    log.info(f"{config.MARROW_NAME} backend stopped.")


async def _execute_user_task(text: str) -> None:
    """
    Execute a task typed by the user in the control bar chat.
    Runs on the asyncio loop, called from Qt thread via run_coroutine_threadsafe.
    """
    from actions.executor import execute_action
    from ui.bridge import get_bridge

    bridge = get_bridge()
    bridge.state_changed.emit("acting")
    try:
        mission_result = await _handle_mission_command(text)
        if mission_result is not None:
            bridge.task_response.emit(mission_result)
            bridge.toast_requested.emit(config.MARROW_NAME, mission_result[:200], 4)
            return

        swarm_result = await _handle_swarm_command(text)
        if swarm_result is not None:
            bridge.task_response.emit(swarm_result)
            bridge.toast_requested.emit(config.MARROW_NAME, swarm_result[:200], 4)
            return

        slash_result = _handle_slash_command(text)
        if slash_result is not None:
            bridge.task_response.emit(slash_result)
            bridge.toast_requested.emit(config.MARROW_NAME, slash_result[:200], 4)
            return

        result = await execute_action(text)
        out = (result or "Done.").strip()
        bridge.task_response.emit(out)
        bridge.toast_requested.emit(config.MARROW_NAME, out[:200], 4)
    except Exception as e:
        err = f"Error: {e}"
        log.error(f"User task failed: {e}")
        bridge.task_response.emit(err)
    finally:
        bridge.state_changed.emit("idle")


def _run_asyncio_backend() -> None:
    """Daemon thread that owns the asyncio event loop."""
    global _asyncio_loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _asyncio_loop = loop
    try:
        loop.run_until_complete(_main_async())
    except Exception as e:
        log.error(f"Asyncio backend error: {e}", exc_info=True)
    finally:
        loop.close()


# ─── Qt frontend ──────────────────────────────────────────────────────────────


def _build_tray(app, toggle_cb):
    """Optional system tray icon."""
    if not config.TRAY_ENABLED:
        return None
    try:
        from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QBrush
        from PyQt6.QtWidgets import QSystemTrayIcon, QMenu

        pix = QPixmap(32, 32)
        pix.fill(QColor(0, 0, 0, 0))
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QBrush(QColor(16, 16, 20, 255)))
        p.setPen(QColor(210, 210, 225, 160))
        p.drawEllipse(2, 2, 28, 28)
        p.setBrush(QBrush(QColor(210, 210, 225, 220)))
        p.setPen(QColor(0, 0, 0, 0))
        p.drawEllipse(11, 11, 10, 10)
        p.end()

        icon = QSystemTrayIcon(QIcon(pix), app)
        menu = QMenu()
        menu.setStyleSheet("""
            QMenu {
                background: rgba(14,14,18,248);
                color: rgba(220,220,230,255);
                border: 1px solid rgba(255,255,255,20);
                border-radius: 8px; padding: 4px; font-size: 9pt;
            }
            QMenu::item { padding: 6px 20px; border-radius: 4px; }
            QMenu::item:selected { background: rgba(96,165,250,80); }
        """)
        menu.addAction("Toggle Marrow", toggle_cb)
        menu.addSeparator()
        menu.addAction("Quit", lambda: (_request_shutdown(), app.quit()))
        icon.setContextMenu(menu)
        icon.activated.connect(
            lambda r: (
                toggle_cb() if r == QSystemTrayIcon.ActivationReason.Trigger else None
            )
        )
        icon.setToolTip(f"{config.MARROW_NAME} — ambient intelligence")
        icon.show()
        return icon
    except Exception as e:
        log.warning(f"Tray init failed: {e}")
        return None


def _run_qt() -> None:
    """Build and run the Qt application on the main thread."""
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import Qt, QTimer

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Marrow")

    # Start backend only after QApplication exists on main thread.
    # This avoids Windows DPI/context races and keeps Qt ownership correct.
    backend_thread = {"t": None}

    def _start_backend_once():
        if backend_thread["t"] is not None:
            return
        t = threading.Thread(
            target=_run_asyncio_backend,
            name="marrow-asyncio",
            daemon=True,
        )
        t.start()
        backend_thread["t"] = t

    QTimer.singleShot(0, _start_backend_once)

    # ── UI surface selection ───────────────────────────────────────────────
    ui_mode = (config.UI_MODE or "orb").lower()
    control_bar = None
    dashboard = None
    orb = None
    overlay = None

    if ui_mode == "controlbar":
        from ui.control_bar import MarrowControlBar

        control_bar = MarrowControlBar()
        if config.CONTROL_BAR_AUTO_SHOW:
            control_bar.show()
    else:
        # Default: small orb with optional dashboard (less distracting)
        from ui.orb import MarrowOrb
        from ui.dashboard import MarrowDashboard

        orb = MarrowOrb()
        orb.connect_bridge()
        orb.show()

        dashboard = MarrowDashboard()

        def _toggle_dashboard():
            if dashboard.isVisible():
                dashboard.hide()
            else:
                dashboard.open_near(orb.geometry())

        orb.dashboard_toggle.connect(_toggle_dashboard)

    if config.OVERLAY_ENABLED:
        try:
            from ui.overlay import MarrowOverlay

            overlay = MarrowOverlay()
        except Exception as e:
            log.warning(f"Overlay init failed: {e}")

    # ── Settings panel ────────────────────────────────────────────────────
    settings_panel = [None]  # lazy singleton

    def _show_settings():
        try:
            from ui.settings_panel import MarrowSettingsPanel

            if settings_panel[0] is None:
                settings_panel[0] = MarrowSettingsPanel()
            settings_panel[0].show()
            settings_panel[0].raise_()
        except Exception as e:
            log.warning(f"Settings panel error: {e}")

    # settings entry points
    if orb:
        orb.settings_requested.connect(_show_settings)
    if dashboard:
        dashboard.settings_requested.connect(_show_settings)

    # ── Quit ─────────────────────────────────────────────────────────────
    def _quit():
        _request_shutdown()
        app.quit()

    if orb:
        orb.quit_requested.connect(_quit)

    # ── Text task: connect from Qt main thread (required for signal safety) ──
    try:
        from ui.bridge import get_bridge as _get_bridge

        def _on_text_task_qt(text: str):
            if _asyncio_loop:
                asyncio.run_coroutine_threadsafe(
                    _execute_user_task(text), _asyncio_loop
                )

        _get_bridge().text_task_submitted.connect(_on_text_task_qt)
    except Exception as e:
        log.warning(f"Text task wire failed: {e}")

    # ── Approval dialog ───────────────────────────────────────────────────
    try:
        from ui.bridge import get_bridge
        from ui.approval_dialog import show_approval_dialog

        get_bridge().approval_requested.connect(show_approval_dialog)
    except Exception as e:
        log.warning(f"Approval bridge wire failed: {e}")

    # ── Claim verification cards ──────────────────────────────────────────
    try:
        from ui.bridge import get_bridge as _gb2
        from ui.claim_card import get_claim_card_manager
        import json as _json

        _claim_mgr = get_claim_card_manager()

        def _on_claim_verified(json_str: str):
            try:
                _claim_mgr.show_claim(_json.loads(json_str))
            except Exception as e:
                log.warning(f"Claim card show failed: {e}")

        _gb2().claim_verified.connect(_on_claim_verified)
    except Exception as e:
        log.warning(f"Claim card wire failed: {e}")

    # ── Toast notifications ────────────────────────────────────────────────
    try:
        from ui.bridge import get_bridge
        from ui.toast import get_toast_manager

        toast_mgr = get_toast_manager()

        def _on_toast(title: str, body: str, urgency: int):
            toast_mgr.show(title, body, urgency)

        get_bridge().toast_requested.connect(_on_toast)

        # Also wire message_spoken → toast (shows what Marrow said visually)
        def _on_message_spoken(text: str, urgency: int):
            def _open_context():
                if control_bar is not None:
                    if not control_bar.isVisible():
                        control_bar.show()
                    try:
                        control_bar.open_with_notification_context(text)
                    except Exception:
                        pass
                elif dashboard is not None and orb is not None:
                    if not dashboard.isVisible():
                        dashboard.open_near(orb.geometry())
                    try:
                        dashboard._open_notification_context()
                    except Exception:
                        pass

            toast_mgr.show(
                config.MARROW_NAME,
                text,
                urgency,
                action_label="Open",
                action_callback=_open_context,
            )

        get_bridge().message_spoken.connect(_on_message_spoken)

    except Exception as e:
        log.warning(f"Toast wire failed: {e}")

    # ── Backend shutdown → app quit ───────────────────────────────────────
    def _check_backend():
        if _asyncio_loop and _asyncio_loop.is_closed():
            app.quit()

    checker = QTimer()
    checker.timeout.connect(_check_backend)
    checker.start(1000)

    # ── Tray ─────────────────────────────────────────────────────────────
    def _toggle_surface():
        if control_bar is not None:
            control_bar.toggle_visibility()
        elif dashboard is not None and orb is not None:
            if dashboard.isVisible():
                dashboard.hide()
            else:
                dashboard.open_near(orb.geometry())

    _build_tray(app, _toggle_surface)

    sys.exit(app.exec())


# ─── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    if _ensure_best_runtime():
        return
    _setup_logging()
    # Qt must own the main thread on Windows.
    # Backend starts from inside _run_qt once QApplication is alive.
    _run_qt()


if __name__ == "__main__":
    main()
