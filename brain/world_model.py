"""
World Model - Marrow's understanding of the user's world.

Tracks entities (people, projects, topics), temporal patterns, and maintains
a dynamic model of what's happening.

This is what makes Marrow "blow people away" - it actually understands.
"""

import asyncio
import hashlib
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

import anthropic

import config
from storage import db

log = logging.getLogger(__name__)


class Entity:
    """A tracked entity in the world model."""

    def __init__(self, name: str, entity_type: str):
        self.name = name
        self.entity_type = entity_type  # person, project, topic, file, app
        self.first_seen = time.time()
        self.last_seen = time.time()
        self.mentions = 1
        self.attributes = {}  # key-value facts learned about this entity
        self.related_entities = set()
        self.context_history = []  # recent contexts where this appeared

    def update(self, context: str = "", **attributes):
        self.last_seen = time.time()
        self.mentions += 1
        if context:
            self.context_history.append({"ts": time.time(), "context": context[:200]})
            # Keep last 10 contexts
            self.context_history = self.context_history[-10:]
        for k, v in attributes.items():
            self.attributes[k] = v

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.entity_type,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "mentions": self.mentions,
            "attributes": self.attributes,
            "related": list(self.related_entities)[:5],
        }


class WorldModel:
    """
    Marrow's world model - tracks everything about the user's world.

    This is the "brain" that understands what's happening.
    """

    def __init__(self):
        self.entities: dict[str, Entity] = {}
        self.topics: dict[str, int] = defaultdict(int)  # topic -> weight
        self.current_focus: str = ""  # What's the user currently working on?
        self.activity_patterns = []  # Time-of-day patterns
        self.recent_events = []  # Significant events

        # Load from DB on init
        self._load_from_db()

    def _load_from_db(self):
        """Load world model from persistent storage."""
        try:
            # Load recent observations
            obs = db.get_observations(limit=100)
            for o in obs:
                content = o.get("content", "")
                obs_type = o.get("type", "fact")

                # Extract entities
                self._extract_entities(content, obs_type)

            # Load recent conversations for context
            convs = db.get_recent_conversations(limit=50)
            for c in convs:
                self._extract_entities(c.get("content", ""), "conversation")

            log.info(f"World model loaded: {len(self.entities)} entities")
        except Exception as e:
            log.warning(f"Failed to load world model: {e}")

    def _extract_entities(self, text: str, source: str):
        """Extract entities from text using simple patterns."""
        if not text:
            return

        # Capitalized words (potential names)
        words = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", text)
        for word in words[:5]:  # Limit per text
            if len(word) > 2 and word.lower() not in {
                "the",
                "this",
                "that",
                "python",
                "windows",
            }:
                self._track_entity(word, "mention", source)

        # Project-like patterns
        project_patterns = re.findall(
            r"(?:project|repo|folder|workspace)[:\s]+([a-zA-Z0-9_-]+)", text, re.I
        )
        for p in project_patterns:
            self._track_entity(p, "project", source)

        # File patterns
        files = re.findall(r"([a-zA-Z0-9_/-]+\.(?:py|js|ts|md|txt|pdf|docx))", text)
        for f in files:
            self._track_entity(f, "file", source)

        # App names
        apps = re.findall(r"\[([a-zA-Z0-9_.-]+)\]", text)
        for app in apps:
            self._track_entity(app, "app", source)

    def _track_entity(self, name: str, entity_type: str, context: str = ""):
        """Track an entity."""
        key = f"{entity_type}:{name.lower()}"

        if key not in self.entities:
            self.entities[key] = Entity(name, entity_type)

        self.entities[key].update(context=context)

        # Update topics based on mentions
        if entity_type == "mention":
            self.topics[name.lower()] = self.topics.get(name.lower(), 0) + 1

    def update_from_screen(self, app: str, title: str, focused: str, ocr: str):
        """Update world model from screen capture."""
        # Track the app
        self._track_entity(app, "app", f"screen: {title}")

        # Extract entities from OCR
        if ocr:
            self._extract_entities(ocr, f"screen:{app}")

        # Extract from focused element
        if focused:
            self._extract_entities(focused, f"focused:{app}")

        # Update current focus
        if app and title:
            self.current_focus = f"{app}: {title[:50]}"

    def update_from_transcript(self, text: str):
        """Update world model from voice transcript."""
        self._extract_entities(text, "voice")

        # Look for action items
        if any(w in text.lower() for w in ["deadline", "due", "meeting", "remind"]):
            self.recent_events.append(
                {"ts": time.time(), "type": "action_item", "content": text[:100]}
            )

    def get_entity(self, name: str, entity_type: str = "") -> Optional[Entity]:
        """Get an entity by name and type."""
        if entity_type:
            key = f"{entity_type}:{name.lower()}"
            return self.entities.get(key)

        # Search by name
        for key, e in self.entities.items():
            if name.lower() in e.name.lower():
                return e
        return None

    def get_context_summary(self) -> str:
        """Get a summary of the current world state."""
        lines = ["## World State"]

        # Current focus
        if self.current_focus:
            lines.append(f"**Focus:** {self.current_focus}")

        # Top entities by type
        people = [e for e in self.entities.values() if e.entity_type == "person"]
        projects = [e for e in self.entities.values() if e.entity_type == "project"]

        if people:
            lines.append(f"**People:** {', '.join([e.name for e in people[:5]])}")
        if projects:
            lines.append(f"**Projects:** {', '.join([e.name for e in projects[:5]])}")

        # Recent topics
        if self.topics:
            top_topics = sorted(self.topics.items(), key=lambda x: x[1], reverse=True)[
                :5
            ]
            lines.append(f"**Topics:** {', '.join([t[0] for t in top_topics])}")

        return "\n".join(lines)

    def to_prompt_context(self) -> str:
        """Convert world model to prompt-injectable context."""
        lines = ["## Current World State"]

        # What app is user in?
        active_apps = [
            e
            for e in self.entities.values()
            if e.entity_type == "app" and time.time() - e.last_seen < 300
        ]
        if active_apps:
            lines.append(f"Active apps: {', '.join([e.name for e in active_apps[:3]])}")

        # What are they working on?
        if self.current_focus:
            lines.append(f"Current focus: {self.current_focus}")

        # Recent important entities
        important = [
            e
            for e in self.entities.values()
            if e.mentions > 2 and time.time() - e.last_seen < 3600
        ]
        if important:
            lines.append("Recently active:")
            for e in important[:5]:
                attrs = (
                    f" ({', '.join(f'{k}={v}' for k, v in list(e.attributes.items())[:2])})"
                    if e.attributes
                    else ""
                )
                lines.append(f"  - {e.name}{attrs}")

        # Recent events
        if self.recent_events:
            lines.append("Recent events:")
            for ev in self.recent_events[-3:]:
                ts = datetime.fromtimestamp(ev["ts"]).strftime("%H:%M")
                lines.append(f"  - {ts}: {ev['content'][:60]}")

        return "\n".join(lines)


# Global world model instance
_world_model: Optional[WorldModel] = None


def get_world_model() -> WorldModel:
    global _world_model
    if _world_model is None:
        _world_model = WorldModel()
    return _world_model


def update_world_from_screen(app: str, title: str, focused: str, ocr: str):
    """Convenience function to update world from screen."""
    get_world_model().update_from_screen(app, title, focused, ocr)


def update_world_from_transcript(text: str):
    """Convenience function to update world from transcript."""
    get_world_model().update_from_transcript(text)


def get_world_context() -> str:
    """Get world model as prompt context."""
    return get_world_model().to_prompt_context()
