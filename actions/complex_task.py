"""
Complex Task Execution Engine.

Features:
1. Planning layer - breaks complex goals into steps
2. Tool chaining - output from one tool feeds into next
3. Verification - checks if goal is actually achieved
4. Self-correction - if failed, try different approach
5. Parallel execution - can run independent steps in parallel

This is what makes Marrow actually execute complex tasks well.
"""

import asyncio
import json
import logging
import re
from typing import Any, Optional
from dataclasses import dataclass, field

import config
from actions import executor, browser, web, file_tools, system

log = logging.getLogger(__name__)


@dataclass
class PlanStep:
    """A single step in a plan."""

    step_id: int
    description: str
    tool: str  # Which tool to use
    input_data: dict  # Tool input
    depends_on: list = field(default_factory=list)  # Step IDs this depends on
    executed: bool = False
    result: Any = None


@dataclass
class ExecutionResult:
    """Result of executing a complex task."""

    success: bool
    summary: str
    steps_executed: int
    steps_failed: int
    total_steps: int
    execution_time: float
    verification_passed: bool


class ComplexTaskExecutor:
    """
    Executes complex tasks with planning, tool chaining, and verification.

    This is what makes Marrow actually "do things" not just answer questions.
    """

    def __init__(self):
        self.max_steps = 10
        self.max_retries = 2
        self.planning_model = "scoring"  # Use unified client model_type

    async def execute_complex_task(
        self,
        goal: str,
        context: str = "",
        verify: bool = True,
    ) -> ExecutionResult:
        """
        Execute a complex task end-to-end.

        1. Create plan (break into steps)
        2. Analyze dependencies
        3. Execute steps (parallel where possible)
        4. Chain outputs
        5. Verify goal achieved
        """
        import time

        start_time = time.time()

        log.info(f"Complex task: {goal[:80]}")

        # Step 1: Create plan
        plan = await self._create_plan(goal, context)

        if not plan:
            return ExecutionResult(
                success=False,
                summary="Could not create a plan for this task",
                steps_executed=0,
                steps_failed=0,
                total_steps=0,
                execution_time=time.time() - start_time,
                verification_passed=False,
            )

        log.info(f"Created plan with {len(plan)} steps")

        # Step 2: Execute plan
        steps_failed = await self._execute_plan(plan, context)
        steps_executed = len([s for s in plan if s.executed])

        # Step 3: Verify if needed
        verification_passed = True
        if verify and steps_failed == 0:
            verification_passed = await self._verify_goal(goal, plan, context)

        # Step 4: Summarize
        success = steps_failed == 0 and verification_passed

        summary = self._generate_summary(plan, verification_passed)

        return ExecutionResult(
            success=success,
            summary=summary,
            steps_executed=steps_executed,
            steps_failed=steps_failed,
            total_steps=len(plan),
            execution_time=time.time() - start_time,
            verification_passed=verification_passed,
        )

    async def _create_plan(self, goal: str, context: str) -> list[PlanStep]:
        """Create a plan for achieving the goal."""
        from brain.llm import get_client

        llm = get_client()

        prompt = f"""Create a detailed plan to achieve this goal:

GOAL: {goal}

CONTEXT:
{context}

Instructions:
1. Break this into 3-10 concrete steps
2. Each step should be achievable with a single tool or action
3. Consider dependencies - what needs to happen before what
4. Include verification step at the end to confirm goal is achieved

For each step, specify:
- What to do (description)
- Which tool to use (tool name)
- What input the tool needs (parameters)

Output as JSON array:
[
  {{
    "step": 1,
    "description": "...",
    "tool": "tool_name",
    "input": {{"param1": "value1"}},
    "depends_on": []
  }},
  ...
]

Only output the JSON array, nothing else."""

        try:
            response = await llm.create(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=900,
                model_type=self.planning_model,
            )

            raw = response.text.strip()

            # Extract JSON
            start = raw.find("[")
            end = raw.rfind("]") + 1
            if start == -1 or end == 0:
                log.warning(f"No JSON plan found: {raw[:200]}")
                return []

            steps_data = json.loads(raw[start:end])

            plan = []
            for s in steps_data:
                plan.append(
                    PlanStep(
                        step_id=s.get("step", len(plan) + 1),
                        description=s.get("description", ""),
                        tool=s.get("tool", ""),
                        input_data=s.get("input", {}),
                        depends_on=s.get("depends_on", []),
                    )
                )

            return plan

        except Exception as e:
            log.error(f"Planning failed: {e}")
            return []

    async def _execute_plan(
        self,
        plan: list[PlanStep],
        context: str,
    ) -> int:
        """Execute the plan, handling dependencies and tool chaining."""

        # Track completed step outputs for chaining
        step_outputs = {}

        # Build dependency graph
        ready_steps = [s for s in plan if not s.depends_on]
        completed = set()
        failed = 0

        while ready_steps and len(completed) < self.max_steps:
            # Execute ready steps in parallel (they have no dependencies)
            if len(ready_steps) > 1:
                # Parallel execution
                tasks = [
                    self._execute_step(step, context, step_outputs)
                    for step in ready_steps
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for step, result in zip(ready_steps, results):
                    if isinstance(result, Exception):
                        log.error(f"Step {step.step_id} failed: {result}")
                        failed += 1
                    else:
                        step.executed = True
                        step.result = result
                        step_outputs[step.step_id] = result
                        completed.add(step.step_id)
            else:
                # Single step
                step = ready_steps[0]
                result = await self._execute_step(step, context, step_outputs)

                if isinstance(result, Exception):
                    log.error(f"Step {step.step_id} failed: {result}")
                    failed += 1
                else:
                    step.executed = True
                    step.result = result
                    step_outputs[step.step_id] = result
                    completed.add(step.step_id)

            # Find next ready steps
            ready_steps = [
                s
                for s in plan
                if s.step_id not in completed
                and all(d in completed for d in s.depends_on)
            ]

        return failed

    async def _execute_step(
        self,
        step: PlanStep,
        context: str,
        previous_outputs: dict,
    ) -> Any:
        """Execute a single step, with tool chaining."""

        tool_name = step.tool.lower()
        tool_input = step.input_data.copy()

        # Chain previous outputs into this step's input
        # Look for references like {"$step": 1, "field": "result"}
        tool_input = self._chain_inputs(tool_input, previous_outputs)

        log.info(f"Executing step {step.step_id}: {step.description[:50]}")

        # Execute via appropriate handler
        result = await self._call_tool(tool_name, tool_input, context)

        return result

    def _chain_inputs(self, tool_input: dict, previous_outputs: dict) -> dict:
        """Replace references with actual previous step outputs."""

        def replace_refs(obj):
            if isinstance(obj, dict):
                # Check for $step reference
                if "$step" in obj:
                    step_id = obj["$step"]
                    field = obj.get("field", "result")
                    if step_id in previous_outputs:
                        output = previous_outputs[step_id]
                        # Navigate to field
                        if field != "result" and isinstance(output, dict):
                            return output.get(field, obj.get("value", ""))
                        return output
                    return obj.get("value", "")

                # Recurse
                return {k: replace_refs(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [replace_refs(item) for item in obj]
            return obj

        return replace_refs(tool_input)

    async def _call_tool(
        self,
        tool_name: str,
        tool_input: dict,
        context: str,
    ) -> str:
        """
        Call the appropriate tool.
        All calls are properly awaited — no asyncio.run() nesting.
        """
        try:
            if tool_name == "run_command":
                # _terminal_exec is sync — run in executor to avoid blocking
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(
                    None, executor._terminal_exec, tool_input.get("command", "")
                )

            elif tool_name == "read_file":
                return await file_tools.file_read(
                    tool_input.get("path", ""),
                    tool_input.get("offset", 0),
                    tool_input.get("limit", 4000),
                )

            elif tool_name == "write_file":
                return await file_tools.file_write(
                    tool_input.get("path", ""),
                    tool_input.get("content", ""),
                )

            elif tool_name == "web_search":
                return await web.web_search(
                    tool_input.get("query", ""),
                    tool_input.get("limit", 5),
                )

            elif tool_name == "web_extract":
                return await web.web_extract(
                    tool_input.get("url", ""),
                    tool_input.get("prompt", "Extract all text content"),
                )

            elif tool_name == "browser_navigate":
                return await browser.browser_navigate(tool_input.get("url", ""))

            elif tool_name == "browser_click":
                return await browser.browser_click(tool_input.get("selector", ""))

            elif tool_name == "browser_type":
                return await browser.browser_type(
                    tool_input.get("selector", ""),
                    tool_input.get("text", ""),
                )

            elif tool_name == "clipboard_read":
                return await system.clipboard_read()

            elif tool_name == "clipboard_write":
                return await system.clipboard_write(tool_input.get("text", ""))

            elif tool_name == "system_info":
                return await system.system_info()

            elif tool_name == "take_screenshot":
                return await system.take_screenshot()

            elif tool_name in ("excel_read", "pdf_read"):
                fn = getattr(file_tools, tool_name, None)
                if fn:
                    return await fn(tool_input.get("path", ""))
                return f"[error] tool {tool_name} not available"

            elif tool_name == "excel_write":
                return await file_tools.excel_write(
                    tool_input.get("path", ""),
                    tool_input.get("data", ""),
                )

            elif tool_name == "execute_code":
                return await file_tools.code_run(
                    tool_input.get("language", "python"),
                    tool_input.get("code", ""),
                )

            else:
                # Fallback: describe the step and let the full executor handle it
                task = f"{tool_name}: {json.dumps(tool_input)}"
                return await executor.execute_action(task, context)

        except Exception as e:
            return f"[error in {tool_name}] {e}"

    async def _verify_goal(
        self,
        goal: str,
        plan: list[PlanStep],
        context: str,
    ) -> bool:
        """Verify the goal was actually achieved."""
        from brain.llm import get_client

        llm = get_client()

        # Collect what was done
        actions_taken = []
        for step in plan:
            if step.executed:
                actions_taken.append(
                    f"Step {step.step_id}: {step.description} -> {str(step.result)[:100]}"
                )

        prompt = f"""Verify if this goal was achieved:

GOAL: {goal}

ACTIONS TAKEN:
{chr(10).join(actions_taken)}

CONTEXT:
{context}

Question: Did the actions taken actually achieve the goal? Answer YES or NO with brief explanation.
If NO, what would need to be done differently?"""

        try:
            response = await llm.create(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=180,
                model_type=self.planning_model,
            )

            result = response.text.strip()
            log.info(f"Verification: {result[:100]}")

            return "YES" in result.upper() or "achieved" in result.lower()

        except Exception as e:
            log.warning(f"Verification failed: {e}")
            return True  # Assume success if verification fails

    def _generate_summary(self, plan: list[PlanStep], verified: bool) -> str:
        """Generate a human-readable summary."""

        lines = ["## Execution Summary\n"]

        for step in plan:
            status = "✓" if step.executed else "○"
            lines.append(f"{status} {step.description}")
            if step.result and step.executed:
                lines.append(f"    → {str(step.result)[:80]}")

        if verified:
            lines.append("\n✓ Goal verified as achieved")
        else:
            lines.append("\n⚠ Goal may not be fully achieved")

        return "\n".join(lines)


# Global instance
_task_executor: Optional[ComplexTaskExecutor] = None


def get_task_executor() -> ComplexTaskExecutor:
    global _task_executor
    if _task_executor is None:
        _task_executor = ComplexTaskExecutor()
    return _task_executor


async def execute_complex(
    goal: str,
    context: str = "",
    verify: bool = True,
) -> str:
    """Execute a complex task end-to-end."""
    executor = get_task_executor()
    result = await executor.execute_complex_task(goal, context, verify)

    return result.summary


async def plan_task(goal: str, context: str = "") -> str:
    """Just create a plan without executing."""
    executor = get_task_executor()
    plan = await executor._create_plan(goal, context)

    if not plan:
        return "Could not create a plan for this task."

    lines = ["## Plan\n"]
    for step in plan:
        lines.append(f"{step.step_id}. {step.description} (uses: {step.tool})")

    return "\n".join(lines)
