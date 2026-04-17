"""
Startup welcome sequence + user-configurable startup tasks.

Fires once per calendar day, ~15 seconds after Marrow starts.

Flow:
  1. Welcome the user (greeting + emails + calendar briefing)
  2. Run any tasks the user has configured in ~/.marrow/startup_tasks.json

User-configurable tasks (~/.marrow/startup_tasks.json):
  [
    {
      "task": "Check my GitHub notifications and summarize open PRs needing review",
      "delay_seconds": 5,
      "enabled": true
    },
    {
      "task": "Open Spotify and play my focus playlist",
      "delay_seconds": 10,
      "enabled": true
    },
    {
      "task": "Check the weather for today and tell me if I need an umbrella",
      "delay_seconds": 0,
      "enabled": false
    }
  ]

Marrow will execute each task in sequence after the welcome greeting.
The executor has full access to run_command, browser, execute_code etc —
so any task that can be described in English can be run.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, date
from pathlib import Path

log = logging.getLogger(__name__)

_STAMP_FILE = Path.home() / ".marrow" / "last_welcome"
_TASKS_FILE = Path.home() / ".marrow" / "startup_tasks.json"
_EXAMPLE_TASKS = Path.home() / ".marrow" / "startup_tasks.example.json"

# Default example tasks written on first run so users know the format
_EXAMPLE_CONTENT = [
    {
        "task": "Check my emails from the last 24 hours and summarize important ones as a toast notification",
        "delay_seconds": 0,
        "enabled": True,
        "description": "Email briefing — summarizes unread emails",
    },
    {
        "task": "Get today's calendar events and show them in a toast notification",
        "delay_seconds": 2,
        "enabled": True,
        "description": "Calendar briefing — shows today's meetings",
    },
    {
        "task": "Check GitHub notifications for PRs or issues that need my attention and notify me",
        "delay_seconds": 5,
        "enabled": False,
        "description": "GitHub PR review checker (requires gh CLI)",
    },
    {
        "task": "Search the web for today's top tech news and give me a 2-sentence summary",
        "delay_seconds": 8,
        "enabled": False,
        "description": "Morning tech news briefing",
    },
    {
        "task": "Open Spotify and start playing my liked songs",
        "delay_seconds": 0,
        "enabled": False,
        "description": "Start focus music on startup",
    },
]


# ─── Stamp helpers ─────────────────────────────────────────────────────────────


def _welcomed_today() -> bool:
    try:
        if _STAMP_FILE.exists():
            ts = float(_STAMP_FILE.read_text().strip())
            return datetime.fromtimestamp(ts).date() == date.today()
    except Exception:
        pass
    return False


def _record_welcome() -> None:
    try:
        _STAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STAMP_FILE.write_text(str(time.time()))
    except Exception as e:
        log.debug(f"Could not write welcome stamp: {e}")


# ─── Task file helpers ─────────────────────────────────────────────────────────


def _ensure_example_file() -> None:
    """Write startup_tasks.example.json on first run so users know the format."""
    try:
        if not _EXAMPLE_TASKS.exists():
            _EXAMPLE_TASKS.parent.mkdir(parents=True, exist_ok=True)
            _EXAMPLE_TASKS.write_text(
                json.dumps(_EXAMPLE_CONTENT, indent=2), encoding="utf-8"
            )
            log.info(f"Startup task examples written to {_EXAMPLE_TASKS}")
    except Exception:
        pass


def _load_startup_tasks() -> list[dict]:
    """Load user's startup_tasks.json. Returns [] if missing or malformed."""
    try:
        if _TASKS_FILE.exists():
            raw = json.loads(_TASKS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return [
                    t for t in raw if isinstance(t, dict) and t.get("enabled", True)
                ]
    except Exception as e:
        log.warning(f"Could not load startup_tasks.json: {e}")
    return []


# ─── Main sequence ─────────────────────────────────────────────────────────────


async def run_startup_sequence(interrupt_engine=None) -> None:
    """
    Called once at startup. Waits 15s, then:
    1. Runs the welcome greeting (once per day)
    2. Runs all enabled tasks from startup_tasks.json
    """
    await asyncio.sleep(15)

    _ensure_example_file()

    if _welcomed_today():
        log.debug("Already welcomed today — skipping startup sequence")
        return

    log.info("Running startup welcome sequence")
    _record_welcome()

    # 1 — Welcome greeting
    try:
        await _run_welcome()
    except Exception as e:
        log.error(f"Welcome greeting error: {e}")

    # 2 — User-configured startup tasks
    tasks = _load_startup_tasks()
    if tasks:
        log.info(f"Running {len(tasks)} startup task(s)")
        for task_cfg in tasks:
            try:
                delay = int(task_cfg.get("delay_seconds", 0))
                if delay > 0:
                    await asyncio.sleep(delay)
                task_text = task_cfg.get("task", "").strip()
                if task_text:
                    desc = task_cfg.get("description", task_text[:60])
                    log.info(f"Startup task: {desc}")
                    asyncio.create_task(_run_task(task_text))
            except Exception as e:
                log.error(f"Startup task error: {e}")


# ─── Welcome greeting ──────────────────────────────────────────────────────────


async def _run_welcome() -> None:
    """Run the personalized welcome greeting via the executor."""
    from actions.executor import execute_action
    from voice.speak import speak
    import config

    user_name = _get_user_name()
    time_of_day = _time_of_day()
    name_part = f" {user_name}" if user_name else ""

    task = f"""
Greet the user as they start their {time_of_day}.

Their name is: {user_name or "unknown (just say hey)"}

Steps:
1. Use memory_search("user preferences schedule priorities") to recall what you know about them.
2. Compose a warm, specific, brief greeting (2-3 sentences max).
   - Say "Good {time_of_day}{name_part}."
   - Mention one relevant thing from memory if you have it (a project they were working on, a meeting pattern, etc.)
   - Keep it personal and direct, not generic.
   - End with a concrete offer to help, not a repeated yes/no question.
3. Use notify_user with title="{config.MARROW_NAME}" and urgency=4 to show the greeting as a toast.
4. Return the greeting text.

Style: like Jarvis greeting Tony. Warm, competent, specific. Never robotic.
Example: "Good morning. You were in the middle of the auth module yesterday. I can reopen it and continue from the next step."
""".strip()

    try:
        result = await execute_action(task, context=f"Startup at {time_of_day}")
        if result and result.strip() and result.strip() != "Done.":
            try:
                await speak(result[:400])
            except Exception:
                pass  # toast already shown by notify_user
    except Exception as e:
        log.error(f"Welcome task error: {e}")
        # Fallback: simple toast
        try:
            from ui.bridge import get_bridge
            import config

            get_bridge().toast_requested.emit(
                config.MARROW_NAME,
                f"Good {time_of_day}{name_part}. Ready when you are.",
                4,
            )
        except Exception:
            pass


async def _run_task(task: str) -> None:
    """Run a single startup task via the executor."""
    from actions.executor import execute_action

    try:
        result = await execute_action(task, context="Startup task")
        log.info(f"Startup task done: {result[:80] if result else 'no output'}")
    except Exception as e:
        log.error(f"Startup task failed: {e}")


# ─── Utilities ─────────────────────────────────────────────────────────────────


def _get_user_name() -> str:
    """Get user's name from memory observations or OS."""
    try:
        from storage import db

        for obs in db.get_observations_by_type("identity", limit=10):
            content = obs.get("content", "")
            if "name" in content.lower():
                for w in content.split():
                    if (
                        w[0].isupper()
                        and len(w) > 2
                        and w.lower() not in ("name", "the", "user", "my", "his", "her")
                    ):
                        return w
    except Exception:
        pass
    try:
        import os

        return (os.environ.get("USERNAME") or os.environ.get("USER") or "").capitalize()
    except Exception:
        return ""


def _time_of_day() -> str:
    h = datetime.now().hour
    if h < 12:
        return "morning"
    elif h < 17:
        return "afternoon"
    else:
        return "evening"
