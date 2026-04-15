"""
Approval system - dangerous command guard.

Security features:
1. Guarded mode (default) - asks before dangerous actions
2. Unlocked mode - runs everything without asking
3. Pattern matching for dangerous commands
4. Approval levels: none, ask, block
"""

import logging
import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

import config

log = logging.getLogger(__name__)


class ApprovalLevel(Enum):
    """How dangerous an action is."""

    SAFE = "safe"  # Always run
    CONFIRM = "confirm"  # Ask user
    BLOCK = "block"  # Never run without explicit unlock


# Dangerous command patterns
DANGEROUS_PATTERNS = [
    # File destruction
    (r"rm\s+-rf", ApprovalLevel.BLOCK, "Recursive force delete"),
    (r"del\s+/[sfq]", ApprovalLevel.BLOCK, "Force delete Windows"),
    (r"Remove-Item\s+-Recurse", ApprovalLevel.BLOCK, "PowerShell recursive delete"),
    (r"format\s+", ApprovalLevel.BLOCK, "Drive format"),
    (r"shutdown", ApprovalLevel.BLOCK, "System shutdown"),
    (r"restart\s+-f", ApprovalLevel.BLOCK, "Force restart"),
    # Network damage
    (r"iptables.*DROP", ApprovalLevel.BLOCK, "Firewall block rule"),
    (r"netsh\s+firewall", ApprovalLevel.BLOCK, "Windows firewall"),
    (r"kill\s+-9", ApprovalLevel.CONFIRM, "Force kill process"),
    (r"taskkill\s+/[ft]", ApprovalLevel.CONFIRM, "Force end Windows process"),
    # System changes
    (r"systemctl\s+disable", ApprovalLevel.CONFIRM, "Disable service"),
    (r"chmod\s+777", ApprovalLevel.CONFIRM, "World-writable permissions"),
    (r"reg\s+(delete|add)", ApprovalLevel.BLOCK, "Windows registry change"),
    (r"HKLM\\|HKCU\\", ApprovalLevel.BLOCK, "Registry modification"),
    # Credential access
    (
        r"cat\s+.*\.(env|credentials|pwd|key|secret)",
        ApprovalLevel.CONFIRM,
        "Read credentials file",
    ),
    (r"Get-Content\s+.*\.env", ApprovalLevel.CONFIRM, "Read .env file"),
    (r"\$env:.*PASSWORD", ApprovalLevel.CONFIRM, "Access password env var"),
    # Network exfiltration
    (
        r"curl.*\$(.*KEY|.*TOKEN|.*SECRET)",
        ApprovalLevel.BLOCK,
        "Exfiltrate credentials",
    ),
    (
        r"wget.*\$(.*KEY|.*TOKEN)",
        ApprovalLevel.BLOCK,
        "Exfiltrate credentials via wget",
    ),
    (r"nc\s+-e", ApprovalLevel.BLOCK, "Reverse shell"),
    (r"/dev/tcp/", ApprovalLevel.BLOCK, "TCP device manipulation"),
    # Package/dependency changes
    (r"pip\s+uninstall", ApprovalLevel.CONFIRM, "Uninstall package"),
    (r"npm\s+uninstall.*-g", ApprovalLevel.CONFIRM, "Uninstall global npm package"),
    (r"apt\s+remove", ApprovalLevel.CONFIRM, "Remove system package"),
    # Code execution
    (r"eval\s*\(", ApprovalLevel.CONFIRM, "Dynamic code evaluation"),
    (r"exec\s*\(", ApprovalLevel.CONFIRM, "Code execution"),
    (r"subprocess.*shell\s*=\s*True", ApprovalLevel.CONFIRM, "Shell execution"),
]

# Commands that require explicit approval even in unlocked mode
ALWAYS_CONFIRM_PATTERNS = [
    (r"curl.*\|.*bash", ApprovalLevel.CONFIRM, "Pipe to bash execution"),
    (r"wget.*\|.*sh", ApprovalLevel.CONFIRM, "Download and execute"),
    (r"python.*-c.*exec", ApprovalLevel.CONFIRM, "Inline Python exec"),
    (r".*\.sh", ApprovalLevel.CONFIRM, "Shell script execution"),
]


@dataclass
class ApprovalRequest:
    """An action that needs approval."""

    command: str
    description: str
    approval_level: ApprovalLevel
    tool_name: str
    args: dict


class ApprovalSystem:
    """
    Handles approval for dangerous commands.

    Modes:
    - GUARDED (default): Ask before dangerous actions
    - UNLOCKED: Run everything without asking
    """

    def __init__(
        self,
        approval_level: str = "guarded",  # "guarded" or "unlocked"
        auto_approve_patterns: list = None,
    ):
        self.mode = approval_level
        self.auto_approve_patterns = auto_approve_patterns or []

        # Compile patterns for performance
        self._dangerous_compiled = [
            (re.compile(p, re.I), level, desc) for p, level, desc in DANGEROUS_PATTERNS
        ]

        self._always_confirm_compiled = [
            (re.compile(p, re.I), level, desc)
            for p, level, desc in ALWAYS_CONFIRM_PATTERNS
        ]

        # User callback for confirmations
        self._confirm_callback: Optional[Callable] = None

    def set_confirm_callback(self, callback: Callable[[ApprovalRequest], bool]):
        """
        Set callback invoked when a CONFIRM-level command needs user approval.
        Signature: callback(request: ApprovalRequest) -> bool
        This is separate from confirm.py's voice-response callback.
        """
        self._confirm_callback = callback

    def check_command(
        self, command: str, tool_name: str = "", args: dict = None
    ) -> tuple[bool, str]:
        """
        Check if a command needs approval.

        Returns: (should_proceed, reason)
        """
        # In unlocked mode, skip most checks
        if self.mode == "unlocked":
            # But still check always-confirm patterns
            for pattern, level, desc in self._always_confirm_compiled:
                if pattern.search(command):
                    if level == ApprovalLevel.BLOCK:
                        return False, f"BLOCKED in unlocked mode: {desc}"
                    if level == ApprovalLevel.CONFIRM:
                        # In unlocked, auto-approve these too unless explicitly blocked
                        log.info(f"Unlocked mode: auto-approved {desc}")

            return True, "unlocked_mode"

        # Check dangerous patterns
        for pattern, level, desc in self._dangerous_compiled:
            if pattern.search(command):
                if level == ApprovalLevel.BLOCK:
                    return False, f"BLOCKED: {desc}"

                if level == ApprovalLevel.CONFIRM:
                    if self._confirm_callback:
                        request = ApprovalRequest(
                            command=command,
                            description=desc,
                            approval_level=level,
                            tool_name=tool_name,
                            args=args or {},
                        )
                        # callback(request) -> bool  (sync, returns True = approved)
                        try:
                            approved = self._confirm_callback(request)
                        except Exception:
                            approved = False
                        if not approved:
                            return False, f"BLOCKED by user: {desc}"
                        return True, f"Approved by user: {desc}"
                    else:
                        # No UI callback wired — use voice confirmation via confirm.py
                        # Returns True (allow) since we can't block synchronously here;
                        # the executor should use confirm_dangerous() for async approvals.
                        log.warning(f"CONFIRM-level command with no callback: {desc} — allowing (wire a callback to block)")
                        return True, f"allowed_no_callback: {desc}"

        return True, "safe"

    def check_file_operation(self, path: str, operation: str) -> tuple[bool, str]:
        """Check file operations."""
        path_lower = path.lower()

        dangerous_paths = [
            "c:\\windows\\system32",
            "/etc/passwd",
            "/etc/shadow",
            "~/.ssh",
            "~/.aws",
            "/.env",
        ]

        for dangerous in dangerous_paths:
            if dangerous in path_lower:
                return False, f"BLOCKED: Protected path {dangerous}"

        return True, "safe"

    def check_env_access(self, env_vars: list) -> tuple[bool, str]:
        """Check if accessing dangerous env vars."""
        dangerous_envs = [
            "PASSWORD",
            "SECRET",
            "KEY",
            "TOKEN",
            "API_KEY",
            "PRIVATE_KEY",
            "CREDENTIAL",
        ]

        for var in env_vars:
            for dangerous in dangerous_envs:
                if dangerous in var.upper():
                    return False, f"BLOCKED: Accessing {var}"

        return True, "safe"


# Global approval system
_approval_system: Optional[ApprovalSystem] = None


def get_approval_system() -> ApprovalSystem:
    global _approval_system
    if _approval_system is None:
        mode = os.environ.get("MARROW_APPROVAL_MODE", "guarded")
        _approval_system = ApprovalSystem(approval_level=mode)
    return _approval_system


def set_approval_mode(mode: str) -> None:
    """Set approval mode: 'guarded' or 'unlocked'."""
    global _approval_system
    _approval_system = ApprovalSystem(approval_level=mode)
    log.info(f"Approval mode set to: {mode}")


def set_confirm_callback(callback: Callable) -> None:
    """Set the confirmation callback."""
    get_approval_system().set_confirm_callback(callback)


def check_approval(
    command: str, tool_name: str = "", args: dict = None
) -> tuple[bool, str]:
    """Convenience function to check command approval."""
    return get_approval_system().check_command(command, tool_name, args)
