"""
Voice input - actual voice commands.

Listens for voice input after wake word or activation,
transcribes, and processes as a command.
"""

import asyncio
import logging
import time
from typing import Optional, Callable

import config

log = logging.getLogger(__name__)


class VoiceInput:
    """
    Handles voice commands.

    Modes:
    - Wake word only: activates, waits for next speech
    - Continuous: listens for commands after activation

    Process:
    1. Detect activation (wake word or hotkey)
    2. Listen for command (configurable duration)
    3. Transcribe
    4. Process as action
    """

    def __init__(
        self,
        listen_duration: int = 5,  # seconds to listen after activation
        silence_timeout: int = 2,  # stop listening after this much silence
    ):
        self.listen_duration = listen_duration
        self.silence_timeout = silence_timeout
        self._is_listening = False
        self._command_callback: Optional[Callable] = None

    def set_command_callback(self, callback: Callable[[str], None]):
        """Set callback to handle transcribed commands."""
        self._command_callback = callback

    async def listen_for_command(self) -> Optional[str]:
        """
        Listen for a voice command after activation.

        Returns the transcribed command, or None if nothing heard.
        """
        log.info("Listening for voice command...")
        self._is_listening = True

        try:
            # Import audio capture to get transcribed text
            from capture.audio import AudioCaptureService

            # Create a temporary audio service for listening
            # In production, this would integrate with the existing audio capture

            # For now, return None - this requires integration with existing audio
            # The existing audio capture already transcribes, so we can check for commands there

            return None

        finally:
            self._is_listening = False

    async def process_voice_command(self, text: str) -> str:
        """Process a transcribed voice command."""
        if not text:
            return ""

        log.info(f"Processing voice command: {text[:50]}")

        # Check if it's a command to Marrow
        text_lower = text.lower().strip()

        # Commands that start with Marrow's name
        if text_lower.startswith("marrow"):
            # Remove "marrow" and get the actual command
            command = text_lower.replace("marrow", "").strip()
            command = command.replace("hey", "").strip()
            command = command.replace("please", "").strip()
            command = command.replace("can you", "").strip()
            command = command.replace("could you", "").strip()

            if command:
                return command

        # If no "marrow", check for common commands
        command_keywords = [
            "search for",
            "find",
            "look up",
            "open",
            "close",
            "start",
            "send",
            "email",
            "message",
            "create",
            "make",
            "write",
            "read",
            "show",
            "tell me",
            "what is",
            "who is",
            "when is",
            "remind me",
            "set alarm",
            "schedule",
        ]

        for keyword in command_keywords:
            if keyword in text_lower:
                return text

        # Not a clear command - treat as general question
        return text

    def is_listening(self) -> bool:
        """Check if currently listening."""
        return self._is_listening


# Global voice input
_voice_input: Optional[VoiceInput] = None


def get_voice_input() -> VoiceInput:
    global _voice_input
    if _voice_input is None:
        _voice_input = VoiceInput()
    return _voice_input


# Integration with audio capture
async def check_for_voice_command(transcribed_text: str) -> Optional[str]:
    """
    Check transcribed text for voice commands.

    Called by audio capture when new speech is transcribed.
    Returns the command if found, None otherwise.
    """

    voice_input = get_voice_input()

    # Check if this is a command
    text_lower = transcribed_text.lower().strip()

    # Wake word triggers listening mode
    if any(w in text_lower for w in ["marrow", "hey marrow"]):
        log.info(f"Wake word detected, preparing for command: {transcribed_text[:50]}")
        # The audio capture already stores this, the executor will pick it up
        return None

    # If already in listening mode (activated), process as command
    if voice_input.is_listening():
        return await voice_input.process_voice_command(transcribed_text)

    # Check for common command patterns without wake word
    command_patterns = [
        "search for",
        "find me",
        "look up",
        "open the",
        "close the",
        "send a",
        "create a",
        "remind me",
    ]

    for pattern in command_patterns:
        if pattern in text_lower:
            return await voice_input.process_voice_command(transcribed_text)

    return None
