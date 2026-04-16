"""
Delegate tool - parallel subagents with restricted toolsets.

Spawns child agents to handle complex tasks in parallel.
Each child gets isolated context, restricted tools, and own session.

Features:
- Parallel execution (multiple subagents run simultaneously)
- Tool restrictions (subagents can't use certain tools)
- Toolset selection (give subagent specific capabilities)
- Result aggregation
"""

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from brain.llm import get_client

import config

log = logging.getLogger(__name__)


# Tools that subagents must never have
BLOCKED_TOOLS = frozenset(
    [
        "delegate_task",  # No recursive delegation
        "run_background",  # No background processes from subagent
        "set_approval_mode",  # Can't change security settings
        "memory_store_file",  # No file storage in memory
    ]
)

# Toolset definitions - what each subagent type can use
AGENT_TOOLSETS = {
    "research": {
        "description": "Research and information gathering",
        "tools": [
            "web_search",
            "web_extract",
            "web_crawl",
            "read_file",
            "memory_search",
        ],
    },
    "file_ops": {
        "description": "File operations",
        "tools": [
            "read_file",
            "write_file",
            "append_file",
            "list_files",
            "search_files",
        ],
    },
    "code": {
        "description": "Code and development tasks",
        "tools": [
            "read_file",
            "write_file",
            "execute_code",
            "run_command",
            "browser_navigate",
        ],
    },
    "general": {
        "description": "General purpose - most tools",
        "tools": [
            "run_command",
            "read_file",
            "write_file",
            "web_search",
            "web_extract",
            "browser_navigate",
            "browser_click",
            "browser_type",
            "clipboard_read",
            "clipboard_write",
            "system_info",
            "take_screenshot",
            "excel_read",
            "excel_write",
            "pdf_read",
            "word_read",
            "memory_search",
            "memory_add",
        ],
    },
    "quick": {
        "description": "Fast simple tasks - limited tools",
        "tools": ["web_search", "read_file", "clipboard_read", "system_info"],
    },
}


@dataclass
class SubagentResult:
    """Result from a subagent."""

    agent_id: str
    success: bool
    output: str
    tool_calls: int = 0
    error: str = ""


class DelegateTool:
    """
    Spawns parallel subagents for complex tasks.

    Usage:
    - delegate_task(task, subagent_type="research", max_agents=3)
    - delegate_task(task, subagent_type="general", max_agents=1)
    """

    def __init__(self):
        self.max_concurrent = 3
        self.max_iterations = 8

    async def delegate(
        self,
        task: str,
        subagent_type: str = "general",
        max_agents: int = 3,
        context: str = "",
    ) -> str:
        """
        Delegate a task to one or more subagents.

        Args:
            task: What to accomplish
            subagent_type: Type of agent (research, file_ops, code, general, quick)
            max_agents: Max parallel subagents (default 3)
            context: Additional context

        Returns:
            Aggregated results from all subagents
        """
        toolset = AGENT_TOOLSETS.get(subagent_type, AGENT_TOOLSETS["general"])

        log.info(f"Delegating task to {max_agents} {subagent_type} subagent(s)")

        # First, decompose the task
        subtasks = await self._decompose_task(task, max_agents)

        if not subtasks:
            subtasks = [task]

        # Execute subtasks in parallel
        results = await self._execute_parallel(
            subtasks=subtasks,
            toolset=toolset,
            context=context,
        )

        # Aggregate results
        return self._aggregate_results(results, task)

    async def _decompose_task(self, task: str, max_agents: int) -> list[str]:
        """Break task into parallel subtasks."""
        client = get_client()

        try:
            response = await client.create(
                messages=[
                    {
                        "role": "user",
                        "content": f"""Break this task into {max_agents} or fewer INDEPENDENT subtasks that can run in PARALLEL.
Each subtask should be self-contained and NOT depend on other subtasks.

Task: {task}

Output as a JSON array of task strings:
["subtask 1", "subtask 2", ...]""",
                    }
                ],
                model_type="scoring",
                max_tokens=800,
            )

            import json, re

            raw = response.text.strip()
            match = re.search(r"\[.*\]", raw, re.DOTALL)
            if match:
                return json.loads(match.group())

        except Exception as e:
            log.warning(f"Task decomposition failed: {e}")

        return [task]

    async def _execute_parallel(
        self,
        subtasks: list[str],
        toolset: dict,
        context: str,
    ) -> list[SubagentResult]:
        """Execute subtasks in parallel."""

        # Filter tools to only those in toolset
        allowed_tools = toolset.get("tools", [])

        # Create tasks
        tasks = [
            self._run_subagent(
                agent_id=f"subagent_{i}",
                task=subtask,
                allowed_tools=allowed_tools,
                context=context,
            )
            for i, subtask in enumerate(subtasks)
        ]

        # Execute in parallel
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Convert exceptions to failed results
        processed = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                processed.append(
                    SubagentResult(
                        agent_id=f"subagent_{i}",
                        success=False,
                        output="",
                        error=str(result),
                    )
                )
            else:
                processed.append(result)

        return processed

    async def _run_subagent(
        self,
        agent_id: str,
        task: str,
        allowed_tools: list[str],
        context: str,
    ) -> SubagentResult:
        """
        Run a single subagent using the main executor restricted to allowed_tools.

        Instead of reimplementing tool dispatch, we reuse executor.execute_action()
        but only pass the subset of MARROW_TOOLS that match allowed_tools.
        This means subagents actually work — not a stub.
        """
        from actions.executor import MARROW_TOOLS, _async_handle_tool_call
        from brain.llm import get_client

        llm = get_client()

        blocked = BLOCKED_TOOLS
        filtered_tools = [
            t
            for t in MARROW_TOOLS
            if t["name"] in allowed_tools and t["name"] not in blocked
        ]

        system_prompt = f"""You are a focused subagent of Marrow. Complete the specific task given.
Available tools: {", ".join(t["name"] for t in filtered_tools)}
Be concise. Return your result as a brief summary at the end."""

        tool_calls = 0

        async def _tool_handler(name: str, inp: dict) -> str:
            return await _async_handle_tool_call(name, inp, context)

        async def _on_tool_call(name: str, inp: dict, result: str) -> None:
            nonlocal tool_calls
            tool_calls += 1

        try:
            text = await llm.create_with_tools(
                messages=[
                    {
                        "role": "user",
                        "content": f"{context}\n\nTask: {task}" if context else task,
                    }
                ],
                tools=filtered_tools,
                tool_handler=_tool_handler,
                system=system_prompt,
                max_tokens=700,
                model_type="scoring",
                max_iterations=self.max_iterations,
                on_tool_call=_on_tool_call,
            )
            return SubagentResult(
                agent_id=agent_id,
                success=True,
                output=text or "",
                tool_calls=tool_calls,
            )
        except Exception as e:
            return SubagentResult(
                agent_id=agent_id,
                success=False,
                output="",
                error=str(e),
                tool_calls=tool_calls,
            )

    def _aggregate_results(
        self,
        results: list[SubagentResult],
        original_task: str,
    ) -> str:
        """Combine subagent results into final answer."""

        lines = [f"## Delegated Task: {original_task[:60]}...\n"]

        successful = [r for r in results if r.success]
        failed = [r for r in results if not r.success]

        lines.append(f"### Results ({len(successful)}/{len(results)} successful)\n")

        for r in results:
            status = "✓" if r.success else "✗"
            lines.append(f"{status} {r.agent_id}:")
            if r.error:
                lines.append(f"  Error: {r.error}")
            else:
                lines.append(f"  {r.output[:300]}")

        if successful:
            lines.append("\n### Summary\n")
            for r in successful:
                lines.append(f"- {r.output[:200]}")

        return "\n".join(lines)


# Global instance
_delegate_tool: Optional[DelegateTool] = None


def get_delegate_tool() -> DelegateTool:
    global _delegate_tool
    if _delegate_tool is None:
        _delegate_tool = DelegateTool()
    return _delegate_tool


async def delegate_task(
    task: str,
    subagent_type: str = "general",
    max_agents: int = 3,
    context: str = "",
) -> str:
    """Convenience function for delegation."""
    return await get_delegate_tool().delegate(task, subagent_type, max_agents, context)
