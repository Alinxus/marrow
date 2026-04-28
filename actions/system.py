"""
Low-level system helpers for the action layer.

These are cross-platform wrappers around clipboard, process, window, and
screen utilities. When a capability is unavailable on the current OS, they
return a helpful degraded message instead of exploding.
"""

from __future__ import annotations

import json
import logging
import platform
import shutil
import subprocess

log = logging.getLogger(__name__)


def _platform() -> str:
    return platform.system()


def _is_windows() -> bool:
    return _platform() == "Windows"


def _cmd_exists(name: str) -> bool:
    return shutil.which(name) is not None


def _run(cmd: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


async def clipboard_read() -> str:
    try:
        from actions import app_control

        return await app_control.clipboard_get()
    except Exception as e:
        return f"[error] {e}"


async def clipboard_write(text: str) -> str:
    try:
        from actions import app_control

        return await app_control.clipboard_set(text)
    except Exception as e:
        return f"[error] {e}"


async def process_list() -> str:
    """List running processes."""
    try:
        import psutil

        rows = []
        for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
            try:
                mem = getattr(proc.info.get("memory_info"), "rss", 0) / (1024 * 1024)
                rows.append(
                    (
                        float(proc.info.get("cpu_percent") or 0),
                        f"{proc.info.get('name') or 'unknown'} (PID: {proc.info.get('pid')}, CPU: {proc.info.get('cpu_percent') or 0}, RSS: {mem:.1f}MB)",
                    )
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        rows.sort(key=lambda item: item[0], reverse=True)
        return "\n".join(text for _, text in rows[:30]) or "No processes"
    except Exception as e:
        return f"[error] {e}"


async def process_kill(name: str) -> str:
    """Kill a process by name."""
    try:
        clean = (name or "").replace(".exe", "")
        if _is_windows():
            result = _run(["taskkill", "/f", "/im", f"{clean}.exe"], timeout=10)
            out = (result.stdout + result.stderr).strip()
            if result.returncode == 0:
                return f"Killed: {clean}"
            result2 = _run(
                ["powershell", "-NoProfile", "-Command", f"Stop-Process -Name '{clean}' -Force -ErrorAction Stop"],
                timeout=10,
            )
            if result2.returncode == 0:
                return f"Killed: {clean}"
            return f"[error] {out or result2.stderr.strip()}"
        if _cmd_exists("pkill"):
            result = _run(["pkill", "-f", clean], timeout=10)
            if result.returncode in (0, 1):
                return f"Killed: {clean}"
            return f"[error] {(result.stdout + result.stderr).strip()}"
        return "[warning] Process kill needs pkill on this platform."
    except Exception as e:
        return f"[error] {e}"


async def process_start(app: str) -> str:
    """Start an application, URL, or file."""
    try:
        from actions import app_control

        return await app_control.app_launch(app)
    except Exception as e:
        return f"[error] {e}"


async def window_list() -> str:
    try:
        from actions import app_control

        return await app_control.window_list()
    except Exception as e:
        return f"[error] {e}"


async def window_focus(title: str) -> str:
    try:
        from actions import app_control

        return await app_control.window_focus(title)
    except Exception as e:
        return f"[error] {e}"


async def window_minimize(title: str) -> str:
    try:
        from actions import app_control

        return await app_control.window_minimize(title)
    except Exception as e:
        return f"[error] {e}"


async def system_info() -> str:
    """Get system information."""
    try:
        import psutil

        info = {
            "platform": _platform(),
            "CPU": f"{psutil.cpu_percent()}%",
            "Memory": f"{psutil.virtual_memory().percent}%",
            "Disk": f"{psutil.disk_usage('/').percent}%",
            "Battery": psutil.sensors_battery().percent if psutil.sensors_battery() else "N/A",
        }
        return json.dumps(info, indent=2)
    except Exception as e:
        return f"[error] {e}"


async def network_info() -> str:
    """Get network information."""
    try:
        import psutil

        nets = psutil.net_connections()
        return f"Active connections: {len(nets)}"
    except Exception as e:
        return f"[error] {e}"


async def get_time() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def take_screenshot() -> str:
    try:
        from actions import app_control

        return await app_control.screenshot()
    except Exception as e:
        return f"[error] {e}"
