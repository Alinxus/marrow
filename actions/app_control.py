"""
Cross-platform app and system control for the action layer.

This module is intentionally best-effort:
- Windows gets richer native window control.
- macOS/Linux still get launch/focus/close/click/type/clipboard/process control.
- When a capability needs OS accessibility permission or a missing utility,
  we return a helpful message instead of crashing startup/runtime.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import platform
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def _platform() -> str:
    return platform.system()


def _is_windows() -> bool:
    return _platform() == "Windows"


def _is_macos() -> bool:
    return _platform() == "Darwin"


def _is_linux() -> bool:
    return _platform() == "Linux"


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


def _best_output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout or result.stderr or "").strip()


def _err(message: str) -> str:
    return f"[error] {message}"


def _warn(message: str) -> str:
    return f"[warning] {message}"


def _ps_run(ps: str, timeout: int = 10) -> str:
    result = _run(["powershell", "-NoProfile", "-Command", ps], timeout=timeout)
    out = _best_output(result)
    return out or "[no output]"


def _applescript(script: str, timeout: int = 10) -> str:
    result = _run(["osascript", "-e", script], timeout=timeout)
    out = _best_output(result)
    if result.returncode != 0:
        return _err(out or "AppleScript failed")
    return out or "[ok]"


def _normalize_hotkey_parts(keys: str) -> list[str]:
    mapped = {
        "ctrl": "ctrl",
        "control": "ctrl",
        "alt": "alt",
        "option": "option" if _is_macos() else "alt",
        "shift": "shift",
        "cmd": "command" if _is_macos() else "win",
        "command": "command" if _is_macos() else "win",
        "super": "win" if not _is_macos() else "command",
        "win": "win" if not _is_macos() else "command",
        "return": "enter",
        "esc": "escape",
        "pgup": "pageup",
        "pgdn": "pagedown",
    }
    out = []
    for raw in (keys or "").split("+"):
        key = raw.strip().lower()
        if not key:
            continue
        out.append(mapped.get(key, key))
    return out


def _mac_window_script(title: str, action: str) -> str:
    escaped = title.replace('"', '\\"')
    return f'''
tell application "System Events"
    set matched to false
    repeat with p in (application processes whose background only is false)
        try
            if (name of p contains "{escaped}") then
                set frontmost of p to true
                set matched to true
                exit repeat
            end if
            repeat with w in windows of p
                if (name of w contains "{escaped}") then
                    if "{action}" is "focus" then
                        set frontmost of p to true
                    else if "{action}" is "minimize" then
                        set value of attribute "AXMinimized" of w to true
                    else if "{action}" is "maximize" then
                        try
                            perform action "AXZoomWindow" of w
                        end try
                    else if "{action}" is "close" then
                        try
                            perform action "AXClose" of w
                        end try
                    end if
                    set matched to true
                    exit repeat
                end if
            end repeat
        end try
        if matched then exit repeat
    end repeat
    if matched then
        return "Matched: {escaped}"
    end if
end tell
return "Window not found: {escaped}"
'''


def _linux_window_tool() -> str | None:
    for name in ("wmctrl", "xdotool"):
        if _cmd_exists(name):
            return name
    return None


async def window_list() -> str:
    """List open windows or visible apps."""
    try:
        if _is_windows():
            ps = """
$rows = Get-Process | Where-Object { $_.MainWindowTitle -and $_.MainWindowHandle -ne 0 } |
    Select-Object -First 50 ProcessName, MainWindowTitle
if (-not $rows) {
    Write-Output "No windows found"
} else {
    $rows | ForEach-Object { "{0}: {1}" -f $_.ProcessName, $_.MainWindowTitle }
}
"""
            return _ps_run(ps)

        if _is_macos():
            script = '''
tell application "System Events"
    set outputLines to {}
    repeat with p in (application processes whose background only is false)
        try
            set end of outputLines to (name of p)
            repeat with w in windows of p
                try
                    set end of outputLines to ("  - " & name of w)
                end try
            end repeat
        end try
    end repeat
    return outputLines as string
end tell
'''
            return _applescript(script)

        tool = _linux_window_tool()
        if tool == "wmctrl":
            result = _run(["wmctrl", "-l"])
            out = _best_output(result)
            return out or "No windows found"
        if tool == "xdotool":
            result = _run(["xdotool", "search", "--onlyvisible", "--name", ".*"])
            ids = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
            rows = []
            for wid in ids[:50]:
                name_result = _run(["xdotool", "getwindowname", wid])
                name = _best_output(name_result) or wid
                rows.append(name)
            return "\n".join(rows) if rows else "No windows found"

        return _warn("Window listing needs wmctrl or xdotool on Linux.")
    except Exception as e:
        return _err(str(e))


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


async def window_focus(title: str) -> str:
    """Focus a window by title (partial match)."""
    try:
        escaped = title.replace("'", "''").replace('"', '`"')
        if _is_windows():
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
        if _is_macos():
            return _applescript(_mac_window_script(title, "focus"))
        tool = _linux_window_tool()
        if tool == "wmctrl":
            result = _run(["wmctrl", "-a", title])
            return _best_output(result) or f"Focused: {title}"
        if tool == "xdotool":
            result = _run(["xdotool", "search", "--name", title, "windowactivate"])
            return _best_output(result) or f"Focused: {title}"
        return _warn("Window focus needs wmctrl or xdotool on Linux.")
    except Exception as e:
        return _err(str(e))


async def window_move(title: str, x: int, y: int) -> str:
    """Move window to position."""
    try:
        escaped = title.replace("'", "''").replace('"', '`"')
        if _is_windows():
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
        if _is_linux() and _cmd_exists("wmctrl"):
            result = _run(["wmctrl", "-r", title, "-e", f"0,{x},{y},-1,-1"])
            return _best_output(result) or f"Moved: {title} to {x}, {y}"
        return _warn("Window move is currently supported on Windows and Linux with wmctrl.")
    except Exception as e:
        return _err(str(e))


async def window_resize(title: str, width: int, height: int) -> str:
    """Resize window."""
    try:
        escaped = title.replace("'", "''").replace('"', '`"')
        if _is_windows():
            ps = (
                _WIN32_TYPE
                + f"""
$proc = Get-Process | Where-Object {{$_.MainWindowTitle -like "*{escaped}*"}} | Select-Object -First 1
if ($proc -and $proc.MainWindowHandle -ne 0) {{
    [void][Win32]::MoveWindow($proc.MainWindowHandle, 0, 0, {width}, {height}, $true)
    Write-Output "Resized to {width}x{height}"
}} else {{ Write-Output "Window not found" }}
"""
            )
            return _ps_run(ps)
        if _is_linux() and _cmd_exists("wmctrl"):
            result = _run(["wmctrl", "-r", title, "-e", f"0,-1,-1,{width},{height}"])
            return _best_output(result) or f"Resized: {title} to {width}x{height}"
        return _warn("Window resize is currently supported on Windows and Linux with wmctrl.")
    except Exception as e:
        return _err(str(e))


async def window_minimize(title: str) -> str:
    """Minimize window."""
    try:
        escaped = title.replace("'", "''").replace('"', '`"')
        if _is_windows():
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
        if _is_macos():
            return _applescript(_mac_window_script(title, "minimize"))
        if _is_linux() and _cmd_exists("xdotool"):
            result = _run(["xdotool", "search", "--name", title, "windowminimize"])
            return _best_output(result) or f"Minimized: {title}"
        return _warn("Window minimize needs Accessibility on macOS or xdotool on Linux.")
    except Exception as e:
        return _err(str(e))


async def window_maximize(title: str) -> str:
    """Maximize window."""
    try:
        escaped = title.replace("'", "''").replace('"', '`"')
        if _is_windows():
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
        if _is_macos():
            return _applescript(_mac_window_script(title, "maximize"))
        if _is_linux() and _cmd_exists("wmctrl"):
            result = _run(["wmctrl", "-r", title, "-b", "add,maximized_vert,maximized_horz"])
            return _best_output(result) or f"Maximized: {title}"
        return _warn("Window maximize needs Accessibility on macOS or wmctrl on Linux.")
    except Exception as e:
        return _err(str(e))


async def window_close(title: str) -> str:
    """Close window gracefully, force-kill if needed."""
    try:
        escaped = title.replace("'", "''").replace('"', '`"')
        if _is_windows():
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
        if _is_macos():
            return _applescript(_mac_window_script(title, "close"))
        if _is_linux() and _cmd_exists("wmctrl"):
            result = _run(["wmctrl", "-c", title])
            return _best_output(result) or f"Closed: {title}"
        return _warn("Window close needs Accessibility on macOS or wmctrl on Linux.")
    except Exception as e:
        return _err(str(e))


async def app_launch(path: str, arguments: str = "") -> str:
    """Launch an application, document, URL, or executable."""
    try:
        args = [part for part in (arguments or "").split() if part]
        target = path.strip()
        if not target:
            return _err("No application path provided")

        if _is_windows():
            import shutil, ctypes

            # Resolve multi-word friendly names to shell tokens Windows can resolve
            _tokens = {
                "file explorer": "explorer", "files": "explorer",
                "this pc": "explorer", "my computer": "explorer",
                "google chrome": "chrome", "microsoft edge": "msedge",
                "command prompt": "cmd", "windows terminal": "wt",
                "vs code": "code", "visual studio code": "code",
                "task manager": "taskmgr", "control panel": "control",
                "windows settings": "ms-settings:", "settings": "ms-settings:",
                "microsoft store": "ms-windows-store:", "store": "ms-windows-store:",
            }
            shell_target = _tokens.get(target.lower(), target)

            # 1. ShellExecuteW — Windows shell handles paths, URLs, ms- URIs, registered apps
            try:
                ret = ctypes.windll.shell32.ShellExecuteW(
                    None, "open", shell_target,
                    " ".join(args) if args else None,
                    None, 1
                )
                if ret > 32:  # >32 = success
                    return f"Launched: {shell_target}"
            except Exception:
                pass

            # 2. cmd /c start — resolves Start Menu entries and PATH
            try:
                subprocess.Popen(
                    ["cmd", "/c", "start", "", shell_target] + args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=0x08000000,
                )
                return f"Launched: {shell_target}"
            except Exception:
                pass

            # 3. Direct Popen if in PATH
            if shutil.which(shell_target):
                subprocess.Popen(
                    [target] + args,
                    creationflags=0x00000008 | 0x00000200,
                    close_fds=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return f"Launched: {target}"

            # 4. PowerShell fallback
            ps = f'Start-Process "{shell_target}"' + (
                f' -ArgumentList "{arguments}"' if arguments else ""
            )
            return _ps_run(ps)

        if _is_macos():
            if target.startswith(("http://", "https://")):
                result = _run(["open", target])
                return _best_output(result) or f"Opened: {target}"
            if Path(target).exists():
                result = _run(["open", target, *args])
                return _best_output(result) or f"Launched: {target}"
            result = _run(["open", "-a", target, *args])
            return _best_output(result) or f"Launched app: {target}"

        if _is_linux():
            if target.startswith(("http://", "https://")) and _cmd_exists("xdg-open"):
                result = _run(["xdg-open", target])
                return _best_output(result) or f"Opened: {target}"
            if Path(target).exists():
                subprocess.Popen(
                    [target, *args],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
                return f"Launched: {target}"
            if _cmd_exists("gtk-launch"):
                result = _run(["gtk-launch", target])
                if result.returncode == 0:
                    return _best_output(result) or f"Launched app: {target}"
            if _cmd_exists("xdg-open"):
                result = _run(["xdg-open", target])
                return _best_output(result) or f"Opened: {target}"

        subprocess.Popen(
            [target, *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return f"Launched: {target}"
    except Exception as e:
        return _err(str(e))


async def app_close(name: str) -> str:
    """Close an application by name."""
    try:
        clean = (name or "").strip().replace(".exe", "")
        if not clean:
            return _err("No application name provided")

        if _is_windows():
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
    $r = & taskkill /f /im "{clean}.exe" 2>&1
    Write-Output $r
}}
"""
            return _ps_run(ps)

        if _is_macos():
            script = f'tell application "{clean}" to quit'
            result = _applescript(script)
            if not result.startswith("[error]"):
                return f"Closed: {clean}"
        if _cmd_exists("pkill"):
            result = _run(["pkill", "-f", clean])
            if result.returncode in (0, 1):
                return f"Closed: {clean}"
        return _warn(f"Could not verify close for {clean}")
    except Exception as e:
        return _err(str(e))


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
    except Exception:
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
    """Focus a window and verify by checking open windows contains the title."""
    out = await window_focus(title)
    await asyncio.sleep(0.25)
    listing = await window_list()
    if title.lower() in (listing or "").lower():
        return f"{out}\nVerified: window match exists in open windows"
    return f"{out}\n[warning] Focus verification uncertain for title '{title}'"


def _try_pyautogui():
    try:
        import pyautogui

        return pyautogui
    except Exception:
        return None


async def mouse_move(x: int, y: int) -> str:
    """Move mouse to position."""
    try:
        pg = _try_pyautogui()
        if pg:
            pg.moveTo(x, y, duration=0.1)
            return f"Moved mouse to {x}, {y}"
        if _is_windows():
            import ctypes

            ctypes.windll.user32.SetCursorPos(x, y)
            return f"Moved mouse to {x}, {y}"
        return _warn(
            "Mouse control needs pyautogui on this platform and Accessibility permission on macOS."
        )
    except Exception as e:
        return _err(str(e))


async def mouse_click(x: int | None = None, y: int | None = None, button: str = "left") -> str:
    """Click at position."""
    try:
        pg = _try_pyautogui()
        if pg:
            if x is not None and y is not None:
                pg.click(x, y, button=button)
            else:
                pg.click(button=button)
            return f"Clicked {button}"
        if _is_windows():
            import ctypes

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
        return _warn(
            "Mouse click needs pyautogui on this platform and Accessibility permission on macOS."
        )
    except Exception as e:
        return _err(str(e))


async def mouse_double_click(x: int | None = None, y: int | None = None) -> str:
    """Double click."""
    try:
        pg = _try_pyautogui()
        if pg:
            if x is not None and y is not None:
                pg.doubleClick(x, y)
            else:
                pg.doubleClick()
            return "Double clicked"
        if _is_windows():
            import ctypes

            if x is not None and y is not None:
                ctypes.windll.user32.SetCursorPos(x, y)
            for _ in range(2):
                ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
                ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
            return "Double clicked"
        return _warn(
            "Double click needs pyautogui on this platform and Accessibility permission on macOS."
        )
    except Exception as e:
        return _err(str(e))


async def mouse_drag(x1: int, y1: int, x2: int, y2: int) -> str:
    """Drag from one position to another."""
    try:
        pg = _try_pyautogui()
        if pg:
            pg.moveTo(x1, y1)
            pg.dragTo(x2, y2, duration=0.3)
            return f"Dragged from ({x1}, {y1}) to ({x2}, {y2})"
        if _is_windows():
            import ctypes
            import time

            ctypes.windll.user32.SetCursorPos(x1, y1)
            time.sleep(0.05)
            ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
            time.sleep(0.05)
            ctypes.windll.user32.SetCursorPos(x2, y2)
            time.sleep(0.05)
            ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
            return f"Dragged from ({x1}, {y1}) to ({x2}, {y2})"
        return _warn(
            "Mouse drag needs pyautogui on this platform and Accessibility permission on macOS."
        )
    except Exception as e:
        return _err(str(e))


async def keyboard_type(text: str) -> str:
    """Type text."""
    try:
        pg = _try_pyautogui()
        if pg:
            pg.write(text, interval=0.02)
            return f"Typed: {text[:50]}"
        if _is_windows():
            safe = text.replace("'", "''").replace("{", "{{").replace("}", "}}")
            ps = f"Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait('{safe}')"
            _ps_run(ps)
            return f"Typed: {text[:50]}"
        return _warn(
            "Keyboard typing needs pyautogui on this platform and Accessibility permission on macOS."
        )
    except Exception as e:
        return _err(str(e))


async def keyboard_hotkey(keys: str) -> str:
    """Press hotkey (e.g., ctrl+c, alt+tab, command+space)."""
    try:
        pg = _try_pyautogui()
        if pg:
            parts = _normalize_hotkey_parts(keys)
            pg.hotkey(*parts)
            return f"Pressed: {keys}"
        if _is_windows():
            key_combo = keys.lower()
            key_combo = (
                key_combo.replace("ctrl", "^")
                .replace("alt", "%")
                .replace("shift", "+")
                .replace("win+", "")
            )
            ps = f'Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait("{key_combo}")'
            _ps_run(ps)
            return f"Pressed: {keys}"
        return _warn(
            "Hotkeys need pyautogui on this platform and Accessibility permission on macOS."
        )
    except Exception as e:
        return _err(str(e))


async def keyboard_press(key: str) -> str:
    """Press a single key."""
    try:
        pg = _try_pyautogui()
        if pg:
            pg.press(_normalize_hotkey_parts(key)[0])
            return f"Pressed: {key}"
        if _is_windows():
            ps = f'Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait("{{{key}}}")'
            _ps_run(ps)
            return f"Pressed: {key}"
        return _warn(
            "Key press needs pyautogui on this platform and Accessibility permission on macOS."
        )
    except Exception as e:
        return _err(str(e))


async def system_info() -> str:
    """Get detailed system info."""
    try:
        import psutil

        info = {
            "platform": _platform(),
            "CPU": f"{psutil.cpu_percent()}%",
            "Memory": f"{psutil.virtual_memory().percent}% ({psutil.virtual_memory().used / (1024**3):.1f}GB used)",
            "Disk": f"{psutil.disk_usage('/').percent}%",
            "Battery": psutil.sensors_battery().percent if psutil.sensors_battery() else "N/A",
        }
        return "\n".join([f"{k}: {v}" for k, v in info.items()])
    except Exception as e:
        return _err(str(e))


async def system_reboot() -> str:
    return "[BLOCKED] Cannot reboot system without explicit user approval"


async def system_shutdown() -> str:
    return "[BLOCKED] Cannot shutdown system without explicit user approval"


async def system_sleep() -> str:
    """Put system to sleep."""
    try:
        if _is_windows():
            _run(
                [
                    "powershell",
                    "-Command",
                    "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false)",
                ],
            )
            return "System sleeping"
        if _is_macos():
            result = _run(["pmset", "sleepnow"])
            return _best_output(result) or "System sleeping"
        if _is_linux():
            if _cmd_exists("systemctl"):
                result = _run(["systemctl", "suspend"])
                return _best_output(result) or "System sleeping"
            return _warn("systemctl not found; cannot suspend system on Linux.")
        return _warn("Sleep not implemented on this platform.")
    except Exception as e:
        return _err(str(e))


async def clipboard_get() -> str:
    """Get clipboard content."""
    try:
        if _is_windows():
            result = _run(["powershell", "-NoProfile", "-Command", "Get-Clipboard"], timeout=5)
            return (result.stdout or "").strip()[:2000] or "[empty]"
        if _is_macos():
            result = _run(["pbpaste"], timeout=5)
            return (result.stdout or "").strip()[:2000] or "[empty]"
        if _is_linux():
            if _cmd_exists("wl-paste"):
                result = _run(["wl-paste", "-n"], timeout=5)
                return (result.stdout or "").strip()[:2000] or "[empty]"
            if _cmd_exists("xclip"):
                result = _run(["xclip", "-selection", "clipboard", "-o"], timeout=5)
                return (result.stdout or "").strip()[:2000] or "[empty]"
            if _cmd_exists("xsel"):
                result = _run(["xsel", "--clipboard", "--output"], timeout=5)
                return (result.stdout or "").strip()[:2000] or "[empty]"
            return _warn("Clipboard read on Linux needs wl-paste, xclip, or xsel.")
        return _warn("Clipboard read not implemented on this platform.")
    except Exception as e:
        return _err(str(e))


async def clipboard_set(text: str) -> str:
    """Set clipboard content."""
    try:
        if _is_windows():
            escaped = text.replace("'", "''")
            _run(["powershell", "-NoProfile", "-Command", f"Set-Clipboard -Value '{escaped}'"], timeout=5)
            return f"Copied: {text[:50]}..."
        if _is_macos():
            result = subprocess.run(["pbcopy"], input=text, text=True, capture_output=True, timeout=5)
            if result.returncode == 0:
                return f"Copied: {text[:50]}..."
            return _err(_best_output(result) or "pbcopy failed")
        if _is_linux():
            if _cmd_exists("wl-copy"):
                result = subprocess.run(["wl-copy"], input=text, text=True, capture_output=True, timeout=5)
                if result.returncode == 0:
                    return f"Copied: {text[:50]}..."
            elif _cmd_exists("xclip"):
                result = subprocess.run(
                    ["xclip", "-selection", "clipboard"],
                    input=text,
                    text=True,
                    capture_output=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    return f"Copied: {text[:50]}..."
            elif _cmd_exists("xsel"):
                result = subprocess.run(
                    ["xsel", "--clipboard", "--input"],
                    input=text,
                    text=True,
                    capture_output=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    return f"Copied: {text[:50]}..."
            return _warn("Clipboard write on Linux needs wl-copy, xclip, or xsel.")
        return _warn("Clipboard write not implemented on this platform.")
    except Exception as e:
        return _err(str(e))


async def screenshot() -> str:
    """Take screenshot."""
    try:
        import mss
        from PIL import Image
        import io

        with mss.mss() as sct:
            monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            img = sct.grab(monitor)

        img_pil = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
        buf = io.BytesIO()
        img_pil.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()

        return f"[screenshot:{b64[:50]}...]"
    except Exception as e:
        return _err(str(e))


async def screenshot_region(x: int, y: int, width: int, height: int) -> str:
    """Take screenshot of region."""
    try:
        import mss
        from PIL import Image
        import io

        with mss.mss() as sct:
            monitor = {"left": x, "top": y, "width": width, "height": height}
            img = sct.grab(monitor)

        img_pil = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
        buf = io.BytesIO()
        img_pil.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()

        return f"[screenshot region:{b64[:50]}...]"
    except Exception as e:
        return _err(str(e))


async def ui_click(x: int, y: int) -> str:
    return await mouse_click(x, y)


async def ui_type(text: str) -> str:
    return await keyboard_type(text)


def get_app_tools() -> list:
    """Get app control tools for the executor."""
    return [
        {"name": "window_list", "description": "List all open windows", "input_schema": {"type": "object", "properties": {}}},
        {
            "name": "window_focus",
            "description": "Focus a window by title",
            "input_schema": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
        },
        {
            "name": "window_move",
            "description": "Move window to position",
            "input_schema": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "x": {"type": "integer"}, "y": {"type": "integer"}},
                "required": ["title", "x", "y"],
            },
        },
        {
            "name": "window_resize",
            "description": "Resize window",
            "input_schema": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "width": {"type": "integer"}, "height": {"type": "integer"}},
                "required": ["title", "width", "height"],
            },
        },
        {
            "name": "window_minimize",
            "description": "Minimize window",
            "input_schema": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
        },
        {
            "name": "window_maximize",
            "description": "Maximize window",
            "input_schema": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
        },
        {
            "name": "window_close",
            "description": "Close window",
            "input_schema": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
        },
        {
            "name": "app_launch",
            "description": "Launch an application, URL, file, or app bundle",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "arguments": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "app_close",
            "description": "Close an application",
            "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        },
        {
            "name": "mouse_move",
            "description": "Move mouse to position",
            "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}}, "required": ["x", "y"]},
        },
        {
            "name": "mouse_click",
            "description": "Click at position",
            "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "button": {"type": "string"}}},
        },
        {
            "name": "keyboard_type",
            "description": "Type text",
            "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        },
        {
            "name": "keyboard_hotkey",
            "description": "Press hotkey (e.g., Ctrl+C)",
            "input_schema": {"type": "object", "properties": {"keys": {"type": "string"}}, "required": ["keys"]},
        },
        {"name": "clipboard_get", "description": "Get clipboard content", "input_schema": {"type": "object", "properties": {}}},
        {
            "name": "clipboard_set",
            "description": "Set clipboard content",
            "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        },
        {"name": "screenshot", "description": "Take full screenshot", "input_schema": {"type": "object", "properties": {}}},
        {
            "name": "screenshot_region",
            "description": "Take screenshot of region",
            "input_schema": {
                "type": "object",
                "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "width": {"type": "integer"}, "height": {"type": "integer"}},
                "required": ["x", "y", "width", "height"],
            },
        },
    ]
