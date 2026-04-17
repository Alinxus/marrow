"""
App and system control - full laptop access.

Can control:
- Windows (move, resize, minimize, maximize, focus)
- Mouse/keyboard automation (click, type, hotkeys)
- Application launching and closing
- Process management
- System operations
- Clipboard
- Screenshot capture
- Any application automation via UI automation

This is what makes Marrow truly powerful - it can do ANYTHING on the laptop.
"""

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

import config

log = logging.getLogger(__name__)


# ─── Window Management ────────────────────────────────────────────────────────


async def window_list() -> str:
    """List all open windows."""
    try:
        import psutil

        windows = []
        for proc in psutil.process_iter(["pid", "name", "title"]):
            try:
                if proc.info.get("title"):
                    windows.append(f"{proc.info['name']}: {proc.info['title']}")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        if not windows:
            return "No windows found"

        return "\n".join(windows[:50])
    except Exception as e:
        return f"[error] {e}"


_WIN32_TYPE = r"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32 {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr hWnd, int X, int Y, int nWidth, int nHeight, bool bRepaint);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
}
"@ -ErrorAction SilentlyContinue
"""


def _ps_run(ps: str, timeout: int = 10) -> str:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    out = (result.stdout + result.stderr).strip()
    return out or "[no output]"


async def window_focus(title: str) -> str:
    """Focus a window by title (partial match)."""
    try:
        escaped = title.replace("'", "''").replace('"', '`"')
        ps = (
            _WIN32_TYPE
            + f"""
$proc = Get-Process | Where-Object {{$_.MainWindowTitle -like "*{escaped}*"}} | Select-Object -First 1
if ($proc -and $proc.MainWindowHandle -ne 0) {{
    [void][Win32]::SetForegroundWindow($proc.MainWindowHandle)
    Write-Output "Focused: {escaped}"
}} else {{
    Write-Output "Window not found: {escaped}"
}}
"""
        )
        return _ps_run(ps)
    except Exception as e:
        return f"[error] {e}"


async def window_move(title: str, x: int, y: int) -> str:
    """Move window to position."""
    try:
        escaped = title.replace("'", "''").replace('"', '`"')
        ps = (
            _WIN32_TYPE
            + f"""
$proc = Get-Process | Where-Object {{$_.MainWindowTitle -like "*{escaped}*"}} | Select-Object -First 1
if ($proc -and $proc.MainWindowHandle -ne 0) {{
    [void][Win32]::MoveWindow($proc.MainWindowHandle, {x}, {y}, 800, 600, $true)
    Write-Output "Moved to {x}, {y}"
}} else {{ Write-Output "Window not found" }}
"""
        )
        return _ps_run(ps)
    except Exception as e:
        return f"[error] {e}"


async def window_resize(title: str, width: int, height: int) -> str:
    """Resize window."""
    try:
        escaped = title.replace("'", "''").replace('"', '`"')
        ps = (
            _WIN32_TYPE
            + f"""
$proc = Get-Process | Where-Object {{$_.MainWindowTitle -like "*{escaped}*"}} | Select-Object -First 1
if ($proc -and $proc.MainWindowHandle -ne 0) {{
    $r = $proc.MainWindowHandle
    # Get current position first
    [void][Win32]::MoveWindow($r, 0, 0, {width}, {height}, $true)
    Write-Output "Resized to {width}x{height}"
}} else {{ Write-Output "Window not found" }}
"""
        )
        return _ps_run(ps)
    except Exception as e:
        return f"[error] {e}"


async def window_minimize(title: str) -> str:
    """Minimize window."""
    try:
        escaped = title.replace("'", "''").replace('"', '`"')
        ps = (
            _WIN32_TYPE
            + f"""
$proc = Get-Process | Where-Object {{$_.MainWindowTitle -like "*{escaped}*"}} | Select-Object -First 1
if ($proc -and $proc.MainWindowHandle -ne 0) {{
    [void][Win32]::ShowWindow($proc.MainWindowHandle, 6)
    Write-Output "Minimized: {escaped}"
}} else {{ Write-Output "Not found: {escaped}" }}
"""
        )
        return _ps_run(ps)
    except Exception as e:
        return f"[error] {e}"


async def window_maximize(title: str) -> str:
    """Maximize window."""
    try:
        escaped = title.replace("'", "''").replace('"', '`"')
        ps = (
            _WIN32_TYPE
            + f"""
$proc = Get-Process | Where-Object {{$_.MainWindowTitle -like "*{escaped}*"}} | Select-Object -First 1
if ($proc -and $proc.MainWindowHandle -ne 0) {{
    [void][Win32]::ShowWindow($proc.MainWindowHandle, 3)
    Write-Output "Maximized: {escaped}"
}} else {{ Write-Output "Not found: {escaped}" }}
"""
        )
        return _ps_run(ps)
    except Exception as e:
        return f"[error] {e}"


async def window_close(title: str) -> str:
    """Close window gracefully, force-kill if needed."""
    try:
        escaped = title.replace("'", "''").replace('"', '`"')
        ps = f"""
$p = Get-Process | Where-Object {{$_.MainWindowTitle -like "*{escaped}*"}} | Select-Object -First 1
if ($p) {{
    $closed = $p.CloseMainWindow()
    Start-Sleep -Milliseconds 500
    if (-not $p.HasExited) {{
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        Write-Output "Force closed: {escaped}"
    }} else {{
        Write-Output "Closed: {escaped}"
    }}
}} else {{
    Write-Output "Not found: {escaped}"
}}
"""
        return _ps_run(ps)
    except Exception as e:
        return f"[error] {e}"


# ─── Application Control ─────────────────────────────────────────────────────


async def app_launch(path: str, arguments: str = "") -> str:
    """Launch an application."""
    try:
        DETACHED = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        cmd = [path] + ([arguments] if arguments else [])
        subprocess.Popen(
            cmd,
            creationflags=DETACHED | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return f"Launched: {path}"
    except FileNotFoundError:
        # Try via PowerShell Start-Process as fallback
        ps = f'Start-Process "{path}"' + (
            f' -ArgumentList "{arguments}"' if arguments else ""
        )
        return _ps_run(ps)
    except Exception as e:
        return f"[error] {e}"


async def app_close(name: str) -> str:
    """Close an application by name (graceful then force)."""
    try:
        # Strip .exe if provided, we'll handle both
        clean = name.replace(".exe", "")
        ps = f"""
$procs = Get-Process -Name "{clean}" -ErrorAction SilentlyContinue
if ($procs) {{
    $procs | ForEach-Object {{
        $_.CloseMainWindow() | Out-Null
    }}
    Start-Sleep -Milliseconds 500
    $still = Get-Process -Name "{clean}" -ErrorAction SilentlyContinue
    if ($still) {{
        $still | Stop-Process -Force -ErrorAction SilentlyContinue
        Write-Output "Force closed: {clean}"
    }} else {{
        Write-Output "Closed: {clean}"
    }}
}} else {{
    # Try taskkill as fallback
    $r = & taskkill /f /im "{clean}.exe" 2>&1
    Write-Output $r
}}
"""
        return _ps_run(ps)
    except Exception as e:
        return f"[error] {e}"


async def app_exists(name: str) -> bool:
    """Check if application is running."""
    try:
        import psutil

        target = (name or "").lower().replace(".exe", "")
        for proc in psutil.process_iter(["name"]):
            pname = (proc.info.get("name") or "").lower().replace(".exe", "")
            if target and (target == pname or target in pname or pname in target):
                return True
        return False
    except:
        return False


async def app_launch_verified(path: str, arguments: str = "") -> str:
    """Launch app and verify process appears."""
    out = await app_launch(path, arguments)
    app_name = Path(path).name.replace(".exe", "")
    for _ in range(8):
        if await app_exists(app_name):
            return f"{out}\nVerified: process '{app_name}' is running"
        await asyncio.sleep(0.35)
    return f"{out}\n[warning] Could not verify process '{app_name}' appeared"


async def window_focus_verified(title: str) -> str:
    """Focus a window and verify by checking window list contains the title."""
    out = await window_focus(title)
    await asyncio.sleep(0.25)
    listing = await window_list()
    if title.lower() in (listing or "").lower():
        return f"{out}\nVerified: window match exists in open windows"
    return f"{out}\n[warning] Focus verification uncertain for title '{title}'"


# ─── Mouse Control ───────────────────────────────────────────────────────────


def _try_pyautogui():
    try:
        import pyautogui

        return pyautogui
    except ImportError:
        return None


async def mouse_move(x: int, y: int) -> str:
    """Move mouse to position."""
    try:
        pg = _try_pyautogui()
        if pg:
            pg.moveTo(x, y, duration=0.1)
            return f"Moved mouse to {x}, {y}"
        # Fallback: ctypes SendInput
        import ctypes

        ctypes.windll.user32.SetCursorPos(x, y)
        return f"Moved mouse to {x}, {y}"
    except Exception as e:
        return f"[error] {e}"


async def mouse_click(x: int = None, y: int = None, button: str = "left") -> str:
    """Click at position."""
    try:
        pg = _try_pyautogui()
        if pg:
            if x is not None and y is not None:
                pg.click(x, y, button=button)
            else:
                pg.click(button=button)
            return f"Clicked {button}"
        # Fallback: ctypes mouse_event
        import ctypes

        MOUSEEVENTF_MOVE = 0x0001
        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP = 0x0004
        MOUSEEVENTF_RIGHTDOWN = 0x0008
        MOUSEEVENTF_RIGHTUP = 0x0010
        if x is not None and y is not None:
            ctypes.windll.user32.SetCursorPos(x, y)
        if button == "right":
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
        else:
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        return f"Clicked {button}"
    except Exception as e:
        return f"[error] {e}"


async def mouse_double_click(x: int = None, y: int = None) -> str:
    """Double click."""
    try:
        pg = _try_pyautogui()
        if pg:
            if x is not None and y is not None:
                pg.doubleClick(x, y)
            else:
                pg.doubleClick()
            return "Double clicked"
        import ctypes

        if x is not None and y is not None:
            ctypes.windll.user32.SetCursorPos(x, y)
        for _ in range(2):
            ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
            ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
        return "Double clicked"
    except Exception as e:
        return f"[error] {e}"


async def mouse_drag(x1: int, y1: int, x2: int, y2: int) -> str:
    """Drag from one position to another."""
    try:
        pg = _try_pyautogui()
        if pg:
            pg.moveTo(x1, y1)
            pg.dragTo(x2, y2, duration=0.3)
            return f"Dragged from ({x1}, {y1}) to ({x2}, {y2})"
        import ctypes, time

        ctypes.windll.user32.SetCursorPos(x1, y1)
        time.sleep(0.05)
        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # left down
        time.sleep(0.05)
        ctypes.windll.user32.SetCursorPos(x2, y2)
        time.sleep(0.05)
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # left up
        return f"Dragged from ({x1}, {y1}) to ({x2}, {y2})"
    except Exception as e:
        return f"[error] {e}"


# ─── Keyboard Control ─────────────────────────────────────────────────────────


async def keyboard_type(text: str) -> str:
    """Type text."""
    try:
        pg = _try_pyautogui()
        if pg:
            pg.typewrite(text, interval=0.02)
            return f"Typed: {text[:50]}"
        # Fallback: PowerShell SendKeys (for ASCII text)
        safe = text.replace("'", "''").replace("{", "{{").replace("}", "}}")
        ps = f"Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait('{safe}')"
        _ps_run(ps)
        return f"Typed: {text[:50]}"
    except Exception as e:
        return f"[error] {e}"


async def keyboard_hotkey(keys: str) -> str:
    """Press hotkey (e.g., ctrl+c, alt+tab, win+d)."""
    try:
        pg = _try_pyautogui()
        if pg:
            parts = [k.strip().lower() for k in keys.split("+")]
            pg.hotkey(*parts)
            return f"Pressed: {keys}"
        # Fallback: PowerShell SendKeys
        key_combo = keys.lower()
        key_combo = (
            key_combo.replace("ctrl", "^")
            .replace("alt", "%")
            .replace("shift", "+")
            .replace("win+", "")  # SendKeys doesn't support Win key
        )
        # Remove + separators that aren't modifier prefixes
        ps = f'Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait("{key_combo}")'
        _ps_run(ps)
        return f"Pressed: {keys}"
    except Exception as e:
        return f"[error] {e}"


async def keyboard_press(key: str) -> str:
    """Press a single key."""
    try:
        pg = _try_pyautogui()
        if pg:
            pg.press(key.lower())
            return f"Pressed: {key}"
        ps = f'Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait("{{{key}}}")'
        _ps_run(ps)
        return f"Pressed: {key}"
    except Exception as e:
        return f"[error] {e}"


# ─── System Operations ─────────────────────────────────────────────────────────


async def system_info() -> str:
    """Get detailed system info."""
    try:
        import psutil

        info = {
            "CPU": f"{psutil.cpu_percent()}%",
            "Memory": f"{psutil.virtual_memory().percent}% ({psutil.virtual_memory().used / (1024**3):.1f}GB used)",
            "Disk": f"{psutil.disk_usage('/').percent}%",
            "Battery": psutil.sensors_battery().percent
            if psutil.sensors_battery()
            else "N/A",
            "Uptime": f"{psutil.boot_time()} (booted)",
        }

        return "\n".join([f"{k}: {v}" for k, v in info.items()])
    except Exception as e:
        return f"[error] {e}"


async def system_reboot() -> str:
    """Reboot the system."""
    return "[BLOCKED] Cannot reboot system without explicit user approval"


async def system_shutdown() -> str:
    """Shutdown the system."""
    return "[BLOCKED] Cannot shutdown system without explicit user approval"


async def system_sleep() -> str:
    """Put system to sleep."""
    try:
        subprocess.run(
            [
                "powershell",
                "-Command",
                "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false)",
            ],
            capture_output=True,
            timeout=10,
        )
        return "System sleeping"
    except Exception as e:
        return f"[error] {e}"


# ─── Clipboard ────────────────────────────────────────────────────────────────


async def clipboard_get() -> str:
    """Get clipboard content."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()[:2000] or "[empty]"
    except Exception as e:
        return f"[error] {e}"


async def clipboard_set(text: str) -> str:
    """Set clipboard content."""
    try:
        text = text.replace("'", "''")
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"Set-Clipboard -Value '{text}'"],
            capture_output=True,
            timeout=5,
        )
        return f"Copied: {text[:50]}..."
    except Exception as e:
        return f"[error] {e}"


# ─── Screenshot ────────────────────────────────────────────────────────────────


async def screenshot() -> str:
    """Take screenshot."""
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
        img_pil.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()

        return f"[screenshot:{b64[:50]}...]"
    except Exception as e:
        return f"[error] {e}"


async def screenshot_region(x: int, y: int, width: int, height: int) -> str:
    """Take screenshot of region."""
    try:
        import mss
        import base64

        with mss.mss() as sct:
            monitor = {"left": x, "top": y, "width": width, "height": height}
            img = sct.grab(monitor)

        from PIL import Image
        import io

        img_pil = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
        buf = io.BytesIO()
        img_pil.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()

        return f"[screenshot region:{b64[:50]}...]"
    except Exception as e:
        return f"[error] {e}"


# ─── UI Automation (Advanced) ────────────────────────────────────────────────


async def ui_click(x: int, y: int) -> str:
    """Click at coordinates using UI automation."""
    return await mouse_click(x, y)


async def ui_type(text: str) -> str:
    """Type text using UI automation."""
    return await keyboard_type(text)


# ─── Get all tool definitions for executor ───────────────────────────────────


def get_app_tools() -> list:
    """Get all app control tools for the executor."""
    return [
        # Window management
        {
            "name": "window_list",
            "description": "List all open windows",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "window_focus",
            "description": "Focus a window by title",
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                },
                "required": ["title"],
            },
        },
        {
            "name": "window_move",
            "description": "Move window to position",
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
                "required": ["title", "x", "y"],
            },
        },
        {
            "name": "window_resize",
            "description": "Resize window",
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                },
                "required": ["title", "width", "height"],
            },
        },
        {
            "name": "window_minimize",
            "description": "Minimize window",
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                },
                "required": ["title"],
            },
        },
        {
            "name": "window_maximize",
            "description": "Maximize window",
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                },
                "required": ["title"],
            },
        },
        {
            "name": "window_close",
            "description": "Close window",
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                },
                "required": ["title"],
            },
        },
        # Application control
        {
            "name": "app_launch",
            "description": "Launch an application",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "arguments": {"type": "string"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "app_close",
            "description": "Close an application",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                },
                "required": ["name"],
            },
        },
        # Mouse control
        {
            "name": "mouse_move",
            "description": "Move mouse to position",
            "input_schema": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
                "required": ["x", "y"],
            },
        },
        {
            "name": "mouse_click",
            "description": "Click at position",
            "input_schema": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "button": {"type": "string"},
                },
            },
        },
        # Keyboard control
        {
            "name": "keyboard_type",
            "description": "Type text",
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                },
                "required": ["text"],
            },
        },
        {
            "name": "keyboard_hotkey",
            "description": "Press hotkey (e.g., Ctrl+C)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "keys": {"type": "string"},
                },
                "required": ["keys"],
            },
        },
        # Clipboard
        {
            "name": "clipboard_get",
            "description": "Get clipboard content",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "clipboard_set",
            "description": "Set clipboard content",
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                },
                "required": ["text"],
            },
        },
        # Screenshot
        {
            "name": "screenshot",
            "description": "Take full screenshot",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "screenshot_region",
            "description": "Take screenshot of region",
            "input_schema": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                },
                "required": ["x", "y", "width", "height"],
            },
        },
    ]
