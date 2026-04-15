"""
User confirmation system - ask before dangerous actions.

Uses TTS to ask user, waits for response.
Also supports Windows toast notifications.
"""

import asyncio
import logging
import os
from typing import Callable, Optional

import config

log = logging.getLogger(__name__)


class UserConfirmation:
    """
    Ask user for confirmation before dangerous actions.

    Two paths:
    1. Async voice: speak question via TTS, await voice response callback
    2. Sync toast + fallback: show Windows toast, log to console

    _response_callback: async () -> str  — returns transcribed voice response.
    Wire this to the audio pipeline if you want real voice confirmation.
    Without it, ask() defaults to False (safe: block).
    """

    def __init__(self):
        # async callable() -> str  (transcribed user response)
        self._response_callback: Optional[Callable] = None

    def set_response_callback(self, callback: Callable) -> None:
        """
        Set async callback that returns a transcribed voice response string.
        Signature: async () -> str
        """
        self._response_callback = callback

    async def ask(
        self,
        question: str,
        timeout_seconds: int = 10,
    ) -> bool:
        """
        Ask user a yes/no question via TTS, wait for voice response.
        Falls back to console + False if no callback wired.
        """
        log.info(f"Asking user: {question}")

        try:
            from voice.speak import speak
            await speak(question)
        except Exception as e:
            log.warning(f"TTS failed for confirmation: {e}")
            print(f"\n[Marrow] {question}")

        if self._response_callback:
            try:
                # callback is async () -> str
                response = await asyncio.wait_for(
                    self._response_callback(),
                    timeout=timeout_seconds,
                )
                if isinstance(response, str):
                    return response.lower().strip() in {
                        "yes", "y", "sure", "ok", "okay", "go", "do it", "yep", "yeah"
                    }
            except asyncio.TimeoutError:
                log.info("User confirmation timed out — defaulting to NO")
        else:
            log.warning("No voice response callback set — confirmation defaults to NO")

        return False

    def ask_sync(self, question: str) -> bool:
        """Synchronous version — always returns False (safe default)."""
        log.warning(f"Sync confirmation requested (no voice possible): {question}")
        return False

    async def notify(
        self,
        title: str,
        message: str,
    ) -> None:
        """Show notification to user."""
        try:
            # Windows toast notification
            import subprocess

            ps_script = f"""
            [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
            [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
            
            $template = @"
            <toast>
                <visual>
                    <binding template="ToastText02">
                        <text id="1">{title}</text>
                        <text id="2">{message}</text>
                    </binding>
                </visual>
            </toast>
"@
            
            $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
            $xml.LoadXml($template)
            $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
            [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Marrow").Show($toast)
            """

            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True,
                timeout=5,
            )
        except Exception as e:
            log.warning(f"Notification failed: {e}")
            # Fall back to console
            print(f"\n🔔 {title}: {message}")


# Global confirmation system
_confirmation: Optional[UserConfirmation] = None


def get_confirmation() -> UserConfirmation:
    global _confirmation
    if _confirmation is None:
        _confirmation = UserConfirmation()
    return _confirmation


async def confirm_dangerous(description: str, command: str = "") -> bool:
    """Ask user to confirm a dangerous action."""
    question = (
        f"Do you want me to {description}? This could be dangerous. Say yes or no."
    )
    return await get_confirmation().ask(question)


async def notify_user(title: str, message: str) -> None:
    """Send notification to user."""
    await get_confirmation().notify(title, message)
