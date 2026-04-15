"""
File tools with security checks.

Features:
- Read/write/patch files
- Path traversal protection
- Sensitive path blocking
- Device path blocking
- Max read size limits
"""

import errno
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Optional

import config

log = logging.getLogger(__name__)

DEFAULT_MAX_READ = 100_000
LOCK = threading.Lock()


_BLOCKED_DEVICE_PATHS = frozenset(
    {
        "/dev/zero",
        "/dev/random",
        "/dev/urandom",
        "/dev/full",
        "/dev/stdin",
        "/dev/tty",
        "/dev/console",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/fd/0",
        "/dev/fd/1",
        "/dev/fd/2",
    }
)

_SENSITIVE_PATH_PREFIXES = (
    "/etc/",
    "/boot/",
    "/usr/lib/systemd/",
    "/private/etc/",
    "/private/var/",
    "C:\\Windows\\",
    "C:\\Program Files\\",
    "C:\\Program Files (x86)\\",
)


def _expand_path(path: str) -> Path:
    """Expand ~ and resolve to absolute path."""
    return Path(os.path.expanduser(path)).resolve()


def _is_blocked_device(path: str) -> bool:
    """Check if path is a blocking device."""
    normalized = os.path.expanduser(path)
    if normalized in _BLOCKED_DEVICE_PATHS:
        return True
    if normalized.startswith("/proc/") and normalized.endswith(
        ("/fd/0", "/fd/1", "/fd/2")
    ):
        return True
    return False


def _is_sensitive_path(path: str) -> Optional[str]:
    """Check if path is sensitive. Returns error message if blocked."""
    try:
        resolved = os.path.realpath(os.path.expanduser(path))
    except (OSError, ValueError):
        resolved = path

    for prefix in _SENSITIVE_PATH_PREFIXES:
        if resolved.startswith(prefix) or path.startswith(prefix):
            return f"Refusing to access sensitive system path: {path}"

    return None


def _check_write_sensitive(path: str) -> Optional[str]:
    """Check if path is sensitive for writes."""
    error = _is_sensitive_path(path)
    if error:
        return error + "\nUse run_command with sudo if needed."
    return None


async def file_read(path: str, offset: int = 0, limit: int = 4000) -> str:
    """Read a file."""
    try:
        path_obj = _expand_path(path)

        if _is_blocked_device(str(path_obj)):
            return "[error] Cannot read device path"

        error = _is_sensitive_path(str(path_obj))
        if error:
            return f"[error] {error}"

        if not path_obj.exists():
            return f"[error] File not found: {path}"

        if path_obj.is_dir():
            return f"[error] Cannot read directory: {path}"

        text = path_obj.read_text(encoding="utf-8", errors="replace")

        if len(text) > DEFAULT_MAX_READ and limit > 200:
            return f"[error] File too large ({len(text)} chars). Use offset/limit to read in parts."

        if offset > 0:
            text = text[offset:]

        if limit > 0 and len(text) > limit:
            text = text[:limit]
            text += "\n[... truncated]"

        return text

    except PermissionError:
        return f"[error] Permission denied: {path}"
    except Exception as e:
        return f"[error] {e}"


async def file_write(path: str, content: str, offset: int = 0) -> str:
    """Write to a file."""
    try:
        path_obj = _expand_path(path)

        error = _check_write_sensitive(str(path_obj))
        if error:
            return f"[error] {error}"

        path_obj.parent.mkdir(parents=True, exist_ok=True)

        if offset > 0 and path_obj.exists():
            existing = path_obj.read_text(encoding="utf-8", errors="replace")
            if offset >= len(existing):
                content = existing + "\n" + content
            else:
                content = existing[:offset] + content + existing[offset:]

        path_obj.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} chars to {path}"

    except PermissionError:
        return f"[error] Permission denied: {path}"
    except Exception as e:
        return f"[error] {e}"


async def file_append(path: str, content: str) -> str:
    """Append to a file."""
    try:
        path_obj = _expand_path(path)

        error = _check_write_sensitive(str(path_obj))
        if error:
            return f"[error] {error}"

        path_obj.parent.mkdir(parents=True, exist_ok=True)

        with open(path_obj, "a", encoding="utf-8") as f:
            f.write(content)

        return f"Appended {len(content)} chars to {path}"

    except PermissionError:
        return f"[error] Permission denied: {path}"
    except Exception as e:
        return f"[error] {e}"


async def file_delete(path: str) -> str:
    """Delete a file."""
    try:
        path_obj = _expand_path(path)

        error = _check_write_sensitive(str(path_obj))
        if error:
            return f"[error] {error}"

        if not path_obj.exists():
            return f"[error] File not found: {path}"

        path_obj.unlink()
        return f"Deleted {path}"

    except Exception as e:
        return f"[error] {e}"


async def file_list(
    path: str = ".", pattern: str = "*", recursive: bool = False
) -> str:
    """List files in a directory."""
    try:
        path_obj = _expand_path(path)

        if not path_obj.exists():
            return f"[error] Directory not found: {path}"

        if not path_obj.is_dir():
            return f"[error] Not a directory: {path}"

        if recursive:
            files = list(path_obj.rglob(pattern))
        else:
            files = list(path_obj.glob(pattern))

        files = [f for f in files if f.is_file()][:50]

        output = [str(f.relative_to(path_obj)) for f in files]
        return "\n".join(output) if output else "No files found"

    except Exception as e:
        return f"[error] {e}"


async def file_search(path: str, query: str, extensions: str = "") -> str:
    """Search for text in files."""
    try:
        import subprocess

        path_obj = _expand_path(path)

        if not path_obj.exists():
            return f"[error] Path not found: {path}"

        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            f"(Get-ChildItem -Path '{path_obj}' -Recurse -File -Include '{extensions if extensions else '*'}' | Select-String -Pattern '{query}' -List).Path",
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        files = result.stdout.strip().split("\n")[:20]
        return "\n".join([f for f in files if f])[:2000] or "No matches found"

    except Exception as e:
        return f"[error] {e}"
