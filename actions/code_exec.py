"""Workspace-aware code execution for coding and build tasks."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _workspace_root(workspace: str | None = None) -> Path:
    if workspace:
        return Path(workspace).expanduser().resolve()
    return Path.cwd().resolve()


def _repo_python(workspace: Path) -> str:
    candidates = [
        workspace / ".venv" / "Scripts" / "python.exe",
        workspace / ".venv" / "bin" / "python",
        Path(sys.executable),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _node_binary() -> str | None:
    for name in ("node", "node.exe"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _shell_command(script_path: Path, language: str) -> list[str]:
    low = language.lower()
    if low in ("python", "py"):
        return [_repo_python(script_path.parent), str(script_path)]
    if low in ("javascript", "js", "node"):
        node = _node_binary()
        if not node:
            raise FileNotFoundError("Node.js not installed")
        return [node, str(script_path)]
    if low in ("bash", "sh", "shell"):
        bash = shutil.which("bash")
        if not bash:
            raise FileNotFoundError("bash not available")
        return [bash, str(script_path)]
    if low in ("powershell", "pwsh", "ps1"):
        pwsh = shutil.which("pwsh") or shutil.which("powershell")
        if not pwsh:
            raise FileNotFoundError("PowerShell not available")
        return [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)]
    raise ValueError(f"Unsupported language: {language}")


def _suffix(language: str) -> str:
    return {
        "python": ".py",
        "py": ".py",
        "javascript": ".js",
        "js": ".js",
        "node": ".js",
        "bash": ".sh",
        "sh": ".sh",
        "shell": ".sh",
        "powershell": ".ps1",
        "pwsh": ".ps1",
        "ps1": ".ps1",
    }.get(language.lower(), ".txt")


def _prepare_script(
    language: str,
    code: str,
    workspace: Path,
    filename: str | None = None,
) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    if filename:
        script_path = (workspace / filename).resolve()
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(code, encoding="utf-8")
        return script_path

    fd, path = tempfile.mkstemp(prefix="marrow_exec_", suffix=_suffix(language), dir=str(workspace))
    os.close(fd)
    script_path = Path(path)
    script_path.write_text(code, encoding="utf-8")
    return script_path


async def code_run(
    language: str,
    code: str,
    timeout: int = 60,
    workspace: str | None = None,
    filename: str | None = None,
    args: str = "",
    keep_file: bool = False,
) -> str:
    """Execute code in a real subprocess with workspace support."""

    ws = _workspace_root(workspace)
    script_path = _prepare_script(language, code, ws, filename=filename)
    cmd = _shell_command(script_path, language)
    if args:
        cmd.extend(args.split())

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["MARROW_WORKSPACE"] = str(ws)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(ws),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = (stdout or b"").decode("utf-8", errors="replace")
        error = (stderr or b"").decode("utf-8", errors="replace")
        combined = (output + ("\n" if output and error else "") + error).strip()
        prefix = f"[{language.lower()} exit={proc.returncode}]"
        if not combined:
            combined = "Executed successfully. No output."
        result = f"{prefix}\n{combined}"
        if filename:
            result += f"\n[file] {script_path}"
        return result[:8000]
    except asyncio.TimeoutError:
        return f"[error] Timed out after {timeout}s"
    except FileNotFoundError as exc:
        return f"[error] {exc}"
    except Exception as exc:
        return f"[error] {exc}"
    finally:
        if not keep_file and not filename:
            try:
                script_path.unlink(missing_ok=True)
            except Exception:
                pass


async def eval_expression(expr: str) -> str:
    """Safely evaluate a simple expression."""
    try:
        result = eval(expr, {"__builtins__": {}}, {})
        return str(result)
    except Exception as e:
        return f"[eval error] {e}"
