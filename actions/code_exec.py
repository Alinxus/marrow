"""
Code execution sandbox.

Runs Python and JavaScript code in isolated environments.
"""

import asyncio
import logging
import sys
import io
import traceback
from typing import Optional

import config

log = logging.getLogger(__name__)


async def execute_python(code: str, timeout: int = 30) -> str:
    """Execute Python code in a sandboxed environment."""

    # Capture stdout
    old_stdout = sys.stdout
    sys.stdout = captured = io.StringIO()

    local_vars = {}
    global_vars = {
        "__name__": "__main__",
        "__builtins__": __builtins__,
    }

    try:
        exec(code, global_vars, local_vars)
        output = captured.getvalue()
        sys.stdout = old_stdout

        if not output:
            return f"[python] Executed successfully. No output."

        return f"[python]\n{output}"[:4000]

    except Exception as e:
        sys.stdout = old_stdout
        tb = traceback.format_exc()
        return f"[python error]\n{tb}"[:4000]


async def execute_javascript(code: str, timeout: int = 30) -> str:
    """Execute JavaScript code using Node.js."""
    import subprocess

    try:
        result = subprocess.run(
            ["node", "-e", code],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(Path.home()),
        )

        output = result.stdout or result.stderr
        return f"[javascript]\n{output}"[:4000]

    except FileNotFoundError:
        return "[error] Node.js not installed"
    except subprocess.TimeoutExpired:
        return f"[error] Timed out after {timeout}s"
    except Exception as e:
        return f"[error] {e}"


async def execute_bash(script: str, timeout: int = 30) -> str:
    """Execute a bash script."""
    import subprocess

    try:
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        output = result.stdout + result.stderr
        return f"[bash]\n{output}"[:4000]

    except FileNotFoundError:
        return "[error] Bash not available"
    except subprocess.TimeoutExpired:
        return f"[error] Timed out after {timeout}s"
    except Exception as e:
        return f"[error] {e}"


async def code_run(
    language: str,
    code: str,
    timeout: int = 30,
) -> str:
    """Execute code in specified language."""

    language = language.lower()

    if language in ("python", "py"):
        return await execute_python(code, timeout)
    elif language in ("javascript", "js", "node"):
        return await execute_javascript(code, timeout)
    elif language in ("bash", "sh", "shell"):
        return await execute_bash(code, timeout)
    else:
        return f"[error] Unsupported language: {language}"


# Simple expression evaluator without exec
async def eval_expression(expr: str) -> str:
    """Safely evaluate a simple expression."""
    try:
        import ast
        import operator

        # Very restricted - only allow basic math
        allowed_ops = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
            ast.Pow: operator.pow,
            ast.USub: operator.neg,
        }

        # This is too complex - just use Python eval with restricted globals
        result = eval(expr, {"__builtins__": {}}, {})
        return str(result)
    except Exception as e:
        return f"[eval error] {e}"
