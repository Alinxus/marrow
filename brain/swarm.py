"""Simple multi-agent swarm coordinator for complex tasks."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Optional

import config

log = logging.getLogger(__name__)


def _emit(payload: dict) -> None:
    try:
        from ui.bridge import get_bridge

        get_bridge().agent_update.emit(json.dumps(payload))
    except Exception as exc:
        log.debug(f"Swarm event skipped: {exc}")


@dataclass
class SwarmAgentState:
    role: str
    task: str
    status: str = "queued"
    output: str = ""
    started_at: float = 0.0
    completed_at: float = 0.0


class SwarmCoordinator:
    def __init__(self) -> None:
        self._last_run: dict | None = None

    def status_text(self) -> str:
        if not self._last_run:
            return "No swarm run yet."
        run = self._last_run
        parts = [f"{agent['role']}={agent['status']}" for agent in run.get("agents", [])]
        return f"Swarm {run['swarm_id'][:8]}: " + ", ".join(parts)

    async def run(self, goal: str, context: str = "") -> str:
        from actions.delegate import get_delegate_tool

        if not config.SWARM_ENABLED:
            return "Swarm mode is disabled."

        swarm_id = str(uuid.uuid4())
        roles = [
            SwarmAgentState("research", f"Research the task and collect facts for: {goal}"),
            SwarmAgentState("executor", f"Produce the best execution plan or concrete actions for: {goal}"),
            SwarmAgentState("verifier", f"Check likely failure modes, missing checks, and verification steps for: {goal}"),
        ]
        self._last_run = {
            "swarm_id": swarm_id,
            "goal": goal,
            "started_at": time.time(),
            "agents": [asdict(agent) for agent in roles],
            "summary": "",
        }
        _emit({"swarm_id": swarm_id, "goal": goal, "status": "starting", "agents": self._last_run["agents"]})

        delegate = get_delegate_tool()

        async def _run_agent(agent: SwarmAgentState, subagent_type: str) -> SwarmAgentState:
            agent.status = "running"
            agent.started_at = time.time()
            _emit({"swarm_id": swarm_id, "role": agent.role, "status": agent.status, "task": agent.task})
            try:
                result = await delegate.delegate(
                    agent.task,
                    subagent_type=subagent_type,
                    max_agents=1,
                    context=context,
                )
                agent.output = (result or "").strip()
                agent.status = "done"
            except Exception as exc:
                agent.output = str(exc)
                agent.status = "failed"
            agent.completed_at = time.time()
            _emit(
                {
                    "swarm_id": swarm_id,
                    "role": agent.role,
                    "status": agent.status,
                    "task": agent.task,
                    "output": agent.output[:320],
                }
            )
            return agent

        completed = await asyncio.gather(
            _run_agent(roles[0], "research"),
            _run_agent(roles[1], "general"),
            _run_agent(roles[2], "quick"),
        )

        summary_lines = [f"Swarm goal: {goal}", ""]
        for agent in completed:
            summary_lines.append(f"[{agent.role}] {agent.output[:600] or agent.status}")
            summary_lines.append("")

        summary = "\n".join(summary_lines).strip()
        self._last_run = {
            "swarm_id": swarm_id,
            "goal": goal,
            "started_at": self._last_run["started_at"],
            "completed_at": time.time(),
            "agents": [asdict(agent) for agent in completed],
            "summary": summary,
        }
        _emit(
            {
                "swarm_id": swarm_id,
                "goal": goal,
                "status": "done",
                "agents": self._last_run["agents"],
                "summary": summary[:400],
            }
        )
        return summary


_COORDINATOR: Optional[SwarmCoordinator] = None


def get_swarm_coordinator() -> SwarmCoordinator:
    global _COORDINATOR
    if _COORDINATOR is None:
        _COORDINATOR = SwarmCoordinator()
    return _COORDINATOR
