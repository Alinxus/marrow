"""Capability bootstrap utilities.

When a requested capability/tool/app is missing, this module attempts to:
1) detect what is already available,
2) install missing dependencies where possible,
3) scaffold a local fallback workflow so task execution can continue.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path


KNOWN_CAPABILITIES = {
    "obsidian": {
        "commands": ["obsidian"],
        "winget_id": "Obsidian.Obsidian",
        "fallback": "Use markdown vault under ~/.marrow/capabilities/obsidian_vault",
    },
    "notion": {
        "commands": ["notion"],
        "winget_id": "Notion.Notion",
        "fallback": "Use local markdown notes + browser automation",
    },
    "github": {
        "commands": ["gh", "git"],
        "winget_id": "GitHub.cli",
        "fallback": "Use browser + web APIs via execute_code",
    },
}


def _slugify(text: str) -> str:
    text = (text or "capability").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:64] or "capability"


def _detect_known(requirement: str) -> tuple[str, dict] | tuple[None, None]:
    low = (requirement or "").lower()
    for key, data in KNOWN_CAPABILITIES.items():
        if key in low:
            return key, data
    return None, None


def _command_exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None


async def bootstrap_capability(
    requirement: str,
    install: bool = True,
    create_local_fallback: bool = True,
    run_command=None,
) -> str:
    """
    Ensure capability exists or scaffold fallback.

    Args:
        requirement: user-intent capability string (e.g. "obsidian-like notes")
        install: try package installation
        create_local_fallback: scaffold local fallback workflow
        run_command: callable(command:str, timeout:int)->str for shell execution
    """
    key, info = _detect_known(requirement)
    commands = (info or {}).get("commands", [])

    # 1) Detect existing commands
    available = [c for c in commands if _command_exists(c)]
    if available:
        return f"Capability ready: found {', '.join(available)} for '{requirement}'."

    install_log = ""
    # 2) Attempt install (Windows-friendly winget path)
    if install and info and info.get("winget_id") and run_command is not None:
        pkg = info["winget_id"]
        cmd = (
            f"winget install --id {pkg} -e --silent "
            f"--accept-package-agreements --accept-source-agreements"
        )
        install_log = run_command(cmd, timeout=180)

        # Re-check after install attempt
        available = [c for c in commands if _command_exists(c)]
        if available:
            return (
                f"Capability installed and ready: {', '.join(available)} for '{requirement}'.\n"
                f"Install output:\n{install_log[:700]}"
            )

    # 3) Scaffold local fallback so agent can continue with what exists
    if create_local_fallback:
        base = Path.home() / ".marrow" / "capabilities"
        slug = _slugify(key or requirement)
        cap_dir = base / slug
        cap_dir.mkdir(parents=True, exist_ok=True)

        manifest = {
            "requirement": requirement,
            "known_key": key,
            "created_at": int(time.time()),
            "fallback": (info or {}).get(
                "fallback", "Use run_command/execute_code/browser tools"
            ),
            "install_log": install_log[:1500],
        }
        (cap_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        guide = [
            f"# Capability: {requirement}",
            "",
            "This capability was bootstrapped by Marrow.",
            "",
            "## Next actions",
            "1. Prefer existing tools (run_command, execute_code, browser_*)",
            "2. Use this folder as local state/workspace",
            "3. If app install is needed, retry winget/choco manually",
            "",
            f"Fallback strategy: {(info or {}).get('fallback', 'local workflow')}.",
        ]
        (cap_dir / "README.md").write_text("\n".join(guide), encoding="utf-8")

        return (
            f"Capability '{requirement}' not natively available; bootstrapped local fallback at:\n"
            f"{cap_dir}\n"
            f"Use run_command/execute_code/browser tools against this workspace."
        )

    return f"Capability '{requirement}' unavailable and no fallback requested."
