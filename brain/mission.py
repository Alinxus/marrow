"""Mission mode orchestration with checkpoints and rollback hooks."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import config
from brain import digital_twin
from storage import state_store

log = logging.getLogger(__name__)


class TaskState:
    IDLE = "idle"
    PLANNING = "planning"
    EXECUTING = "executing"
    PAUSED = "paused"
    VERIFYING = "verifying"
    ROLLBACK = "rollback"
    DONE = "done"
    FAILED = "failed"


@dataclass
class MissionStep:
    step_id: int
    title: str
    description: str
    rollback_hint: str = ""
    success_assertion: str = ""
    depends_on: list[int] = field(default_factory=list)
    status: str = "pending"
    retries: int = 0
    eta_seconds: int = 30
    confidence: float = 0.65
    result: str = ""


@dataclass
class MissionRecord:
    mission_id: str
    goal: str
    state: str = TaskState.IDLE
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    completed_at: float = 0.0
    current_step_index: int = 0
    confidence: float = 0.5
    eta_seconds: int = 0
    retries: int = 0
    summary: str = ""
    last_error: str = ""
    steps: list[MissionStep] = field(default_factory=list)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    verification: list[dict[str, Any]] = field(default_factory=list)


def _bridge_emit(topic: str, payload: dict[str, Any]) -> None:
    try:
        from ui.bridge import get_bridge

        bridge = get_bridge()
        if topic == "mission":
            bridge.mission_update.emit(json.dumps(payload))
        elif topic == "verification":
            bridge.verification_update.emit(json.dumps(payload))
        elif topic == "overlay":
            bridge.overlay_update.emit(json.dumps(payload))
        elif topic == "agent":
            bridge.agent_update.emit(json.dumps(payload))
    except Exception as exc:
        log.debug(f"Bridge emit skipped for {topic}: {exc}")


class MissionController:
    def __init__(self) -> None:
        self._current: Optional[MissionRecord] = None
        self._run_task: Optional[asyncio.Task] = None
        self._resume_event = asyncio.Event()
        self._resume_event.set()

    def current(self) -> Optional[MissionRecord]:
        return self._current

    def current_status_text(self) -> str:
        if not self._current:
            return "No active mission."
        mission = self._current
        step = ""
        if mission.steps and 0 <= mission.current_step_index < len(mission.steps):
            active = mission.steps[mission.current_step_index]
            step = f" Current step: {active.step_id}. {active.title} [{active.status}]"
        return (
            f"Mission {mission.mission_id[:8]} is {mission.state}. "
            f"Goal: {mission.goal}.{step}"
        )

    def _serialize(self, mission: MissionRecord) -> dict[str, Any]:
        payload = asdict(mission)
        payload["steps"] = [asdict(step) for step in mission.steps]
        return payload

    def _checkpoint(self, mission: MissionRecord, note: str = "") -> None:
        mission.updated_at = time.time()
        mission.checkpoints.append(
            {
                "ts": mission.updated_at,
                "state": mission.state,
                "current_step_index": mission.current_step_index,
                "note": note,
            }
        )
        if len(mission.checkpoints) > 60:
            del mission.checkpoints[: len(mission.checkpoints) - 60]
        state_store.upsert_mission(self._serialize(mission))
        step = None
        if mission.steps and 0 <= mission.current_step_index < len(mission.steps):
            step = mission.steps[mission.current_step_index]
        event = {
            "mission_id": mission.mission_id,
            "goal": mission.goal,
            "state": mission.state,
            "step_index": mission.current_step_index,
            "step_count": len(mission.steps),
            "confidence": mission.confidence,
            "eta_seconds": mission.eta_seconds,
            "retries": mission.retries,
            "summary": mission.summary,
            "last_error": mission.last_error,
            "step": asdict(step) if step else None,
        }
        _bridge_emit("mission", event)
        _bridge_emit(
            "overlay",
            {
                "kind": "mission",
                "mission_id": mission.mission_id,
                "goal": mission.goal,
                "state": mission.state,
                "current_action": step["title"]
                if isinstance(step, dict)
                else (step.title if step else ""),
                "confidence": mission.confidence,
                "next_step": step.description if step else "",
            },
        )

    @staticmethod
    def _looks_like_failure(text: str) -> bool:
        low = (text or "").lower()
        return (
            (not low.strip())
            or "[error]" in low
            or "failed" in low
            or "not found" in low
            or "unknown tool" in low
            or "traceback" in low
        )

    async def _verify_step_contract(
        self,
        step: MissionStep,
        result: str,
        goal: str,
        context: str,
    ) -> tuple[bool, str, float]:
        if self._looks_like_failure(result):
            return False, "tool result indicates failure", 0.2

        assertion = (step.success_assertion or "").strip()
        if not assertion:
            return True, "no explicit assertion", max(0.5, step.confidence)

        from brain.llm import get_client

        prompt = f"""Evaluate whether this mission step succeeded.

Goal: {goal}
Step: {step.title}
Step action: {step.description}
Success assertion: {assertion}
Result: {result[:1200]}
Context: {context[:900]}

Return JSON only:
{{"passed": true, "confidence": 0.0, "reason": "short"}}
"""
        try:
            llm = get_client()
            response = await llm.create(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=140,
                model_type="scoring",
            )
            raw = (response.text or "").strip()
            start = raw.find("{")
            end = raw.rfind("}") + 1
            payload = json.loads(raw[start:end]) if start != -1 and end > start else {}
            passed = bool(payload.get("passed", False))
            conf = float(payload.get("confidence", 0.5))
            reason = str(payload.get("reason", "")).strip() or "contract evaluated"
            return passed and conf >= 0.5, reason, conf
        except Exception as exc:
            return True, f"contract fallback: {exc}", max(0.45, step.confidence)

    async def _attempt_recovery(
        self,
        step: MissionStep,
        mission: MissionRecord,
        context: str,
        previous_result: str,
    ) -> str:
        from actions.executor import execute_action

        recovery_task = (
            f"Recover a failed mission step. Goal: {mission.goal}. "
            f"Failed step: {step.title}. Intended action: {step.description}. "
            f"Failure details: {previous_result[:700]}. "
            f"Take an alternate approach and complete only this step."
        )
        return await asyncio.wait_for(
            execute_action(recovery_task, context),
            timeout=config.MISSION_STEP_TIMEOUT_SECONDS,
        )

    async def _create_plan(self, goal: str, context: str) -> list[MissionStep]:
        from brain.llm import get_client

        enriched_context = context
        if config.SWARM_ENABLED:
            try:
                from brain.swarm import get_swarm_coordinator

                swarm_summary = await get_swarm_coordinator().run(goal, context=context)
                enriched_context = (
                    context + "\n\nSwarm planning context:\n" + swarm_summary[:2200]
                ).strip()
            except Exception as exc:
                log.debug(f"Mission swarm planning skipped: {exc}")

        prompt = f"""Plan a mission to achieve this goal.

GOAL:
{goal}

CONTEXT:
{enriched_context}

Return JSON only:
[
  {{
    "step_id": 1,
    "title": "short step title",
    "description": "concrete action phrased as an executable instruction",
    "depends_on": [],
    "rollback_hint": "how to reverse this step if needed",
    "success_assertion": "what should be true if this step worked",
    "eta_seconds": 45,
    "confidence": 0.72
  }}
]

Rules:
- 3 to {config.MISSION_MAX_STEPS} steps
- prefer concrete desktop/computer actions
- include rollback hints for anything destructive
- keep each description executable by an action agent
- include a final verification-oriented step
"""
        try:
            llm = get_client()
            response = await llm.create(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1200,
                model_type="reasoning",
            )
            text = (response.text or "").strip()
            start = text.find("[")
            end = text.rfind("]") + 1
            if start == -1 or end <= start:
                raise ValueError("plan JSON not found")
            raw_steps = json.loads(text[start:end])
            steps: list[MissionStep] = []
            for index, row in enumerate(raw_steps[: config.MISSION_MAX_STEPS], start=1):
                steps.append(
                    MissionStep(
                        step_id=int(row.get("step_id", index)),
                        title=(row.get("title") or f"Step {index}").strip(),
                        description=(row.get("description") or "").strip(),
                        rollback_hint=(row.get("rollback_hint") or "").strip(),
                        success_assertion=(row.get("success_assertion") or "").strip(),
                        depends_on=list(row.get("depends_on") or []),
                        eta_seconds=max(15, int(row.get("eta_seconds", 45))),
                        confidence=float(row.get("confidence", 0.65)),
                    )
                )
            return [step for step in steps if step.description]
        except Exception as exc:
            log.warning(f"Mission planning fallback for '{goal[:80]}': {exc}")
            return [
                MissionStep(
                    step_id=1,
                    title="Understand task",
                    description=f"Gather the information and context needed to complete: {goal}",
                    success_assertion="Needed context is available.",
                    eta_seconds=30,
                ),
                MissionStep(
                    step_id=2,
                    title="Execute task",
                    description=goal,
                    rollback_hint="Undo the most recent changes if they were created by this mission.",
                    success_assertion="The requested action is completed.",
                    eta_seconds=60,
                ),
                MissionStep(
                    step_id=3,
                    title="Verify result",
                    description=f"Verify that this goal is satisfied and summarize any remaining risk: {goal}",
                    success_assertion="Verification confirms the mission succeeded.",
                    eta_seconds=30,
                ),
            ]

    async def start_mission(self, goal: str, context: str = "") -> MissionRecord:
        if self._run_task and not self._run_task.done():
            raise RuntimeError(
                "A mission is already running. Pause, resume, or rollback it first."
            )

        mission = MissionRecord(mission_id=str(uuid.uuid4()), goal=goal)
        mission.state = TaskState.PLANNING
        mission.started_at = time.time()
        self._current = mission
        digital_twin.add_task_signal(goal, "planning", mission.mission_id)
        self._checkpoint(mission, "Mission created")
        mission.steps = await self._create_plan(goal, context)
        mission.eta_seconds = sum(step.eta_seconds for step in mission.steps)
        mission.confidence = round(
            sum(step.confidence for step in mission.steps) / max(1, len(mission.steps)),
            2,
        )
        self._checkpoint(mission, "Plan ready")
        self._resume_event.set()
        self._run_task = asyncio.create_task(self._run_mission(mission, context))
        return mission

    async def _run_mission(self, mission: MissionRecord, context: str) -> None:
        from actions.executor import execute_action

        mission.state = TaskState.EXECUTING
        self._checkpoint(mission, "Execution started")

        try:
            start_index = max(0, int(mission.current_step_index))
            for index in range(start_index, len(mission.steps)):
                step = mission.steps[index]
                mission.current_step_index = index
                if step.status == "done":
                    continue
                await self._resume_event.wait()
                if mission.state == TaskState.ROLLBACK:
                    break

                step.status = "running"
                mission.eta_seconds = sum(
                    item.eta_seconds
                    for item in mission.steps[index:]
                    if item.status not in {"done", "skipped"}
                )
                self._checkpoint(mission, f"Running step {step.step_id}")

                try:
                    result = await asyncio.wait_for(
                        execute_action(step.description, context),
                        timeout=config.MISSION_STEP_TIMEOUT_SECONDS,
                    )
                    step_result = (result or "").strip()
                    passed, why, conf = await self._verify_step_contract(
                        step,
                        step_result,
                        mission.goal,
                        context,
                    )
                    if not passed:
                        raise RuntimeError(f"step contract failed: {why}")
                    step.result = step_result
                    step.confidence = max(step.confidence, conf)
                    step.status = "done"
                    mission.summary = step.result[:240]
                    self._checkpoint(mission, f"Completed step {step.step_id}")
                except Exception as exc:
                    step.retries += 1
                    mission.retries += 1
                    step.result = f"Step failed: {exc}"
                    mission.last_error = step.result
                    _bridge_emit(
                        "verification",
                        {
                            "mission_id": mission.mission_id,
                            "step_id": step.step_id,
                            "assertion": step.success_assertion or step.description,
                            "status": "failed",
                            "details": step.result,
                            "confidence": 0.2,
                        },
                    )
                    recovered = False
                    if step.retries <= 1:
                        step.status = "retrying"
                        self._checkpoint(mission, f"Retrying step {step.step_id}")
                        try:
                            retry_result = await asyncio.wait_for(
                                execute_action(step.description, context),
                                timeout=config.MISSION_STEP_TIMEOUT_SECONDS,
                            )
                            retry_text = (retry_result or "").strip()
                            passed, why, conf = await self._verify_step_contract(
                                step,
                                retry_text,
                                mission.goal,
                                context,
                            )
                            if passed:
                                step.result = retry_text
                                step.confidence = max(step.confidence, conf)
                                step.status = "done"
                                recovered = True
                                self._checkpoint(
                                    mission,
                                    f"Completed retry for step {step.step_id}",
                                )
                            else:
                                step.result = f"Retry contract failed: {why}"
                        except Exception as retry_exc:
                            step.result = f"Retry failed: {retry_exc}"

                    if (
                        not recovered
                        and config.MISSION_RECOVERY_ENABLED
                        and step.retries <= 2
                    ):
                        step.status = "retrying"
                        self._checkpoint(
                            mission, f"Recovery strategy for step {step.step_id}"
                        )
                        try:
                            recovery_result = await self._attempt_recovery(
                                step, mission, context, step.result
                            )
                            recovery_text = (recovery_result or "").strip()
                            passed, why, conf = await self._verify_step_contract(
                                step,
                                recovery_text,
                                mission.goal,
                                context,
                            )
                            if passed:
                                step.result = recovery_text
                                step.confidence = max(step.confidence, conf)
                                step.status = "done"
                                recovered = True
                                self._checkpoint(
                                    mission,
                                    f"Recovered step {step.step_id} with alternate strategy",
                                )
                            else:
                                step.result = f"Recovery contract failed: {why}"
                        except Exception as recovery_exc:
                            step.result = f"Recovery failed: {recovery_exc}"

                    if not recovered:
                        step.status = "failed"
                        mission.state = TaskState.FAILED
                        self._checkpoint(
                            mission, f"Mission failed at step {step.step_id}"
                        )
                        digital_twin.add_task_signal(
                            mission.goal, "failed", mission.mission_id
                        )
                        return

                _bridge_emit(
                    "verification",
                    {
                        "mission_id": mission.mission_id,
                        "step_id": step.step_id,
                        "assertion": step.success_assertion or step.description,
                        "status": "passed",
                        "details": step.result[:400],
                        "confidence": min(0.95, max(0.45, step.confidence)),
                    },
                )

            if mission.state != TaskState.ROLLBACK:
                mission.state = TaskState.VERIFYING
                self._checkpoint(mission, "Mission verification")
                if config.MISSION_AUTO_VERIFY:
                    await self._verify_mission(mission, context)
                mission.state = TaskState.DONE
                mission.completed_at = time.time()
                mission.summary = mission.summary or "Mission completed."
                self._checkpoint(mission, "Mission completed")
                digital_twin.add_task_signal(mission.goal, "done", mission.mission_id)
        finally:
            if mission.state not in {TaskState.PAUSED, TaskState.ROLLBACK}:
                self._run_task = None

    async def _verify_mission(self, mission: MissionRecord, context: str) -> None:
        from brain.llm import get_client

        lines = []
        for step in mission.steps:
            lines.append(
                f"{step.step_id}. {step.title}: {step.status} -> {step.result[:160]}"
            )
        prompt = f"""Verify this mission outcome.

GOAL:
{mission.goal}

STEPS:
{chr(10).join(lines)}

CONTEXT:
{context}

Return JSON only:
{{
  "passed": true,
  "confidence": 0.84,
  "summary": "brief result",
  "remaining_risk": "brief risk if any"
}}
"""
        try:
            llm = get_client()
            response = await llm.create(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=260,
                model_type="scoring",
            )
            text = (response.text or "").strip()
            start = text.find("{")
            end = text.rfind("}") + 1
            payload = json.loads(text[start:end]) if start != -1 and end > start else {}
        except Exception as exc:
            payload = {
                "passed": mission.last_error == "",
                "confidence": 0.55 if mission.last_error == "" else 0.35,
                "summary": "Verification fell back to local mission state.",
                "remaining_risk": str(exc),
            }
        mission.confidence = float(
            payload.get("confidence", mission.confidence or 0.55)
        )
        mission.summary = payload.get("summary", mission.summary)
        mission.verification.append(payload)
        _bridge_emit(
            "verification",
            {
                "mission_id": mission.mission_id,
                "step_id": None,
                "assertion": mission.goal,
                "status": "passed" if payload.get("passed", True) else "failed",
                "details": payload.get("remaining_risk", ""),
                "confidence": mission.confidence,
                "summary": mission.summary,
            },
        )

    async def pause_current(self) -> str:
        mission = self._current
        if not mission:
            return "No mission is active."
        mission.state = TaskState.PAUSED
        self._resume_event.clear()
        self._checkpoint(mission, "Paused by user")
        digital_twin.add_task_signal(mission.goal, "paused", mission.mission_id)
        return f"Paused mission {mission.mission_id[:8]}."

    async def resume_current(self) -> str:
        mission = self._current
        if not mission:
            recoverable = self.recover_last_mission()
            if recoverable:
                mission = recoverable
            else:
                return "No paused mission is available."
        mission.state = TaskState.EXECUTING
        self._resume_event.set()
        self._checkpoint(mission, "Resumed by user")
        if self._run_task is None or self._run_task.done():
            self._run_task = asyncio.create_task(self._run_mission(mission, ""))
        digital_twin.add_task_signal(mission.goal, "executing", mission.mission_id)
        return f"Resumed mission {mission.mission_id[:8]}."

    async def rollback_current(self) -> str:
        from actions.executor import execute_action

        mission = self._current
        if not mission:
            return "No mission is active."
        mission.state = TaskState.ROLLBACK
        self._resume_event.set()
        self._checkpoint(mission, "Rollback requested")
        outputs = []
        for step in reversed(mission.steps[: mission.current_step_index + 1]):
            if step.status != "done" or not step.rollback_hint:
                continue
            try:
                result = await asyncio.wait_for(
                    execute_action(step.rollback_hint, ""),
                    timeout=config.MISSION_STEP_TIMEOUT_SECONDS,
                )
                outputs.append(f"{step.step_id}: {result[:120]}")
            except Exception as exc:
                outputs.append(f"{step.step_id}: rollback failed ({exc})")
        mission.state = TaskState.FAILED
        mission.summary = (
            "Rollback executed."
            if outputs
            else "Rollback requested; no rollback hints were available."
        )
        mission.completed_at = time.time()
        self._checkpoint(mission, "Rollback finished")
        digital_twin.add_task_signal(mission.goal, "rolled_back", mission.mission_id)
        self._run_task = None
        return mission.summary + ("" if not outputs else "\n" + "\n".join(outputs))

    def recover_last_mission(self) -> Optional[MissionRecord]:
        missions = state_store.load_missions().get("missions", [])
        for row in reversed(missions):
            if row.get("state") in {
                TaskState.PAUSED,
                TaskState.EXECUTING,
                TaskState.PLANNING,
            }:
                steps = [MissionStep(**step) for step in row.get("steps", [])]
                recovered = MissionRecord(
                    mission_id=row["mission_id"],
                    goal=row["goal"],
                    state=TaskState.PAUSED,
                    created_at=row.get("created_at", time.time()),
                    updated_at=row.get("updated_at", time.time()),
                    started_at=row.get("started_at", 0.0),
                    completed_at=row.get("completed_at", 0.0),
                    current_step_index=row.get("current_step_index", 0),
                    confidence=row.get("confidence", 0.5),
                    eta_seconds=row.get("eta_seconds", 0),
                    retries=row.get("retries", 0),
                    summary=row.get("summary", ""),
                    last_error=row.get("last_error", ""),
                    steps=steps,
                    checkpoints=row.get("checkpoints", []),
                    verification=row.get("verification", []),
                )
                self._current = recovered
                self._resume_event.clear()
                self._checkpoint(recovered, "Recovered paused mission")
                return recovered
        return None


_CONTROLLER: Optional[MissionController] = None


def get_mission_controller() -> MissionController:
    global _CONTROLLER
    if _CONTROLLER is None:
        _CONTROLLER = MissionController()
    return _CONTROLLER
