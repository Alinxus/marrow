"""Platform permission checks for Marrow runtime capabilities."""

from __future__ import annotations

import platform
import subprocess
from typing import Dict, Tuple


def _check_microphone() -> Tuple[bool, str]:
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        has_input = any(int(d.get("max_input_channels", 0)) > 0 for d in devices)
        if has_input:
            return True, "Input device detected"
        return False, "No microphone input device detected"
    except Exception as e:
        return False, f"Microphone check failed: {e}"


def _check_screen_capture() -> Tuple[bool, str]:
    try:
        import mss

        with mss.mss() as sct:
            monitor = sct.monitors[1]
            shot = sct.grab(monitor)
            if shot.width > 0 and shot.height > 0:
                return True, "Screen capture probe succeeded"
        return False, "Screen capture probe returned empty frame"
    except Exception as e:
        return False, f"Screen capture check failed: {e}"


def _check_macos_accessibility() -> Tuple[bool, str]:
    cmd = [
        "osascript",
        "-e",
        'tell application "System Events" to get name of first process',
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
        if p.returncode == 0:
            return True, "System Events accessible"
        msg = (p.stderr or p.stdout or "").strip()
        return False, f"Accessibility denied or unavailable: {msg[:140]}"
    except Exception as e:
        return False, f"Accessibility check failed: {e}"


def _check_hotkey_runtime() -> Tuple[bool, str]:
    try:
        import keyboard  # noqa: F401

        return True, "Hotkey library available"
    except Exception as e:
        return False, f"Hotkey library unavailable: {e}"


def check_permissions(detailed: bool = False) -> str:
    """Return a human-readable permission checklist."""
    sys_name = platform.system()
    checks: Dict[str, Tuple[bool, str]] = {
        "screen_capture": _check_screen_capture(),
        "microphone": _check_microphone(),
        "hotkey": _check_hotkey_runtime(),
    }

    if sys_name == "Darwin":
        checks["accessibility"] = _check_macos_accessibility()

    lines = [f"## Permission Check ({sys_name})"]
    bad = 0
    for k, (ok, msg) in checks.items():
        if not ok:
            bad += 1
        status = "OK" if ok else "MISSING"
        lines.append(f"- {k}: {status} — {msg}")

    if sys_name == "Darwin":
        lines.append(
            "- macOS setup: System Settings > Privacy & Security > Screen Recording, Microphone, Accessibility"
        )

    if bad == 0:
        lines.append("All core permissions/capabilities look healthy.")
    else:
        lines.append(
            f"{bad} issue(s) detected. Marrow will run with degraded capabilities."
        )

    if detailed:
        lines.append(
            "Tip: after changing permissions, fully restart terminal and Marrow process."
        )

    return "\n".join(lines)


def open_permission_panels() -> str:
    """Open OS permission settings panels relevant to Marrow."""
    sys_name = platform.system()

    if sys_name == "Darwin":
        urls = [
            "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        ]
        opened = 0
        for u in urls:
            try:
                p = subprocess.run(
                    ["open", u], capture_output=True, text=True, timeout=6
                )
                if p.returncode == 0:
                    opened += 1
            except Exception:
                pass

        if opened:
            return (
                f"Opened {opened}/{len(urls)} macOS permission panels. "
                "Grant access, then fully restart terminal/app and Marrow."
            )
        return "Failed to open macOS permission panels automatically. Open System Settings > Privacy & Security manually."

    if sys_name == "Windows":
        cmds = [
            ["cmd", "/c", "start", "ms-settings:privacy-microphone"],
            ["cmd", "/c", "start", "ms-settings:privacy-webcam"],
        ]
        opened = 0
        for c in cmds:
            try:
                p = subprocess.run(c, capture_output=True, text=True, timeout=6)
                if p.returncode == 0:
                    opened += 1
            except Exception:
                pass
        return (
            f"Opened {opened}/{len(cmds)} Windows privacy panels. "
            "Check microphone/camera/privacy and app permissions."
        )

    return (
        "Permission panel opener is currently implemented for macOS and Windows only."
    )
