"""
System tools: clipboard, process management, window control, system info.

Provides low-level system access for the action layer.
"""

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

import config

log = logging.getLogger(__name__)


async def clipboard_read() -> str:
    """Read clipboard contents."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or "[empty]"
    except Exception as e:
        return f"[error] {e}"


async def clipboard_write(text: str) -> str:
    """Write to clipboard."""
    try:
        escaped = text.replace("'", "''")
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Set-Clipboard -Value '{escaped}'",
            ],
            capture_output=True,
            timeout=5,
        )
        return f"Wrote to clipboard: {text[:100]}..."
    except Exception as e:
        return f"[error] {e}"


async def process_list() -> str:
    """List running processes."""
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Process | Sort-Object -Property CPU -Descending | Select-Object -First 20 | Format-Table Name, Id, CPU, WorkingSet -AutoSize",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout[:3000] or "No processes"
    except Exception as e:
        return f"[error] {e}"


async def process_kill(name: str) -> str:
    """Kill a process by name."""
    try:
        clean = name.replace(".exe", "")
        result = subprocess.run(
            ["taskkill", "/f", "/im", f"{clean}.exe"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = (result.stdout + result.stderr).strip()
        if result.returncode == 0:
            return f"Killed: {clean}"
        # Fallback to Stop-Process
        result2 = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Stop-Process -Name '{clean}' -Force -ErrorAction Stop"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result2.returncode == 0:
            return f"Killed: {clean}"
        return f"[error] {out or result2.stderr.strip()}"
    except Exception as e:
        return f"[error] {e}"


async def process_start(app: str) -> str:
    """Start an application."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"Start-Process '{app}'"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return f"Started: {app}"
        return f"[error] {result.stderr.strip()}"
    except Exception as e:
        return f"[error] {e}"


async def window_list() -> str:
    """List open windows."""
    try:
        import psutil

        windows = []
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if proc.info["name"]:
                    windows.append(f"{proc.info['name']} (PID: {proc.info['pid']})")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        return "\n".join(windows[:30])
    except Exception as e:
        return f"[error] {e}"


async def window_focus(title: str) -> str:
    """Focus a window by title."""
    try:
        escaped = title.replace("'", "''")
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-Process | Where-Object {{$_.MainWindowTitle -like '*{escaped}*'}} | Select-Object -First 1).MainWindowHandle | ForEach-Object {{[void][Win32]::SetForegroundWindow($_)}}",
            ],
            capture_output=True,
            timeout=5,
        )
        return f"Focused window: {title}"
    except Exception as e:
        return f"[error] {e}"


async def window_minimize(title: str) -> str:
    """Minimize a window."""
    try:
        escaped = title.replace("'", "''")
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-Process | Where-Object {{$_.MainWindowTitle -like '*{escaped}*'}} | Select-Object -First 1).MainWindowHandle | ForEach-Object {{[void][Win32]::ShowWindow($_, 6)}}",
            ],
            capture_output=True,
            timeout=5,
        )
        return f"Minimized: {title}"
    except Exception as e:
        return f"[error] {e}"


async def system_info() -> str:
    """Get system information."""
    try:
        import psutil

        info = {
            "CPU": f"{psutil.cpu_percent()}%",
            "Memory": f"{psutil.virtual_memory().percent}%",
            "Disk": f"{psutil.disk_usage('/').percent}%",
            "Battery": psutil.sensors_battery().percent
            if psutil.sensors_battery()
            else "N/A",
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
    """Get current time."""
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def take_screenshot() -> str:
    """Take a screenshot using mss."""
    try:
        import mss
        import base64

        with mss.mss() as sct:
            monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            img = sct.grab(monitor)

        from PIL import Image
        import io

        img_pil = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
        buf = io.BytesIO()
        img_pil.save(buf, format="JPEG", quality=70)
        b64 = base64.b64encode(buf.getvalue()).decode()

        return f"[screenshot:{b64[:100]}...]"
    except Exception as e:
        return f"[error] {e}"
