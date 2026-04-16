"""Local adapter registry for user-specific capabilities.

Adapters are persistent tool definitions stored on disk and auto-loaded into
the executor on future runs.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Callable


ADAPTERS_DIR = Path.home() / ".marrow" / "adapters"
LEARN_FILE = Path.home() / ".marrow" / "adapter_learning.json"


def _slug(text: str) -> str:
    v = re.sub(r"[^a-z0-9]+", "_", (text or "adapter").lower()).strip("_")
    return v[:48] or "adapter"


def _safe_format(template: str, values: dict) -> str:
    class _Safe(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    return template.format_map(_Safe(values or {}))


def _manifest_path(name: str) -> Path:
    ADAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    return ADAPTERS_DIR / f"{_slug(name)}.json"


def _load_manifest(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def _save_manifest(path: Path, manifest: dict) -> None:
    ADAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _find_manifest_by_slug(slug_key: str) -> tuple[dict | None, Path | None]:
    ADAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    for p in sorted(ADAPTERS_DIR.glob("*.json")):
        m = _load_manifest(p)
        if not m:
            continue
        if _slug(m.get("name", "")) == slug_key:
            return m, p
    return None, None


def _adapter_trust(manifest: dict) -> float:
    runs = int(manifest.get("total_runs", 0))
    success = int(manifest.get("success_runs", 0))
    # Bayesian-smoothed trust so new adapters are not 0/1 extreme
    return (success + 1.0) / (runs + 2.0)


def list_adapters() -> list[dict]:
    ADAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    for p in sorted(ADAPTERS_DIR.glob("*.json")):
        m = _load_manifest(p)
        if m:
            out.append(m)
    return out


def _normalize_task(task: str) -> str:
    s = (task or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return " ".join(s.split()[:10])


def _load_learning() -> dict:
    try:
        if LEARN_FILE.exists():
            data = json.loads(LEARN_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {"patterns": {}}


def _save_learning(data: dict) -> None:
    LEARN_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEARN_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def maybe_suggest_adapter(task: str, threshold: int = 3) -> str:
    """Track repeated tasks and return adapter suggestion when threshold reached."""
    key = _normalize_task(task)
    if not key:
        return ""

    data = _load_learning()
    pats = data.setdefault("patterns", {})
    row = pats.get(key, {"count": 0, "last_suggested": 0})
    row["count"] = int(row.get("count", 0)) + 1
    pats[key] = row
    _save_learning(data)

    now = int(time.time())
    last_suggested = int(row.get("last_suggested", 0))
    if row["count"] >= threshold and (now - last_suggested) > 86400:
        row["last_suggested"] = now
        pats[key] = row
        _save_learning(data)
        return (
            f"You asked for a similar task {row['count']} times: '{key}'. "
            "I can create a reusable local adapter for this workflow."
        )

    return ""


def get_adapter_tools() -> list[dict]:
    """Convert saved adapters into executor tool definitions."""
    tools = []
    for m in list_adapters():
        name = m.get("name", "adapter")
        tool_name = f"adapter_{_slug(name)}"
        description = m.get("description") or f"User adapter: {name}"
        trust = _adapter_trust(m)
        input_schema = m.get("input_schema") or {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "What to do"},
                "context": {"type": "string", "description": "Extra context"},
            },
        }
        tools.append(
            {
                "name": tool_name,
                "description": f"{description} (trust {trust:.2f})",
                "input_schema": input_schema,
            }
        )
    return tools


def recommend_adapter_tool(task: str, min_trust: float = 0.35) -> str:
    """Return best adapter tool for task, if confidence is decent."""
    task_norm = _normalize_task(task)
    if not task_norm:
        return ""
    task_terms = set(task_norm.split())

    best_name = ""
    best_score = 0.0
    for m in list_adapters():
        trust = _adapter_trust(m)
        if trust < min_trust:
            continue

        requirement = (
            m.get("requirement", "") + " " + m.get("description", "")
        ).lower()
        req_terms = set(_normalize_task(requirement).split())
        overlap = len(task_terms & req_terms)
        if overlap == 0:
            continue

        # blend lexical match and historical trust
        score = (min(1.0, overlap / 4.0) * 0.55) + (trust * 0.45)
        if score > best_score:
            best_score = score
            best_name = f"adapter_{_slug(m.get('name', 'adapter'))}"

    return best_name


def create_local_adapter(
    requirement: str,
    adapter_name: str,
    description: str = "",
    mode: str = "command",
    command_template: str = "",
    python_script: str = "",
    input_schema_json: str = "",
) -> str:
    """Create and persist a local adapter for future runs."""
    name = adapter_name or requirement or "adapter"
    mode = (mode or "command").lower().strip()
    if mode not in ("command", "python"):
        mode = "command"

    schema = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Requested task"},
            "context": {"type": "string", "description": "Optional context"},
        },
    }
    if input_schema_json:
        try:
            parsed = json.loads(input_schema_json)
            if isinstance(parsed, dict):
                schema = parsed
        except Exception:
            pass

    if mode == "command" and not command_template:
        command_template = (
            'powershell -NoProfile -Command "Write-Output "Adapter task: {task}""'
        )

    script_path = ""
    if mode == "python":
        script_name = f"{_slug(name)}.py"
        script_path = str((ADAPTERS_DIR / script_name).resolve())
        if not python_script:
            python_script = (
                "import json,sys\n"
                "payload=json.loads(sys.argv[1]) if len(sys.argv)>1 else {}\n"
                "task=payload.get('task','')\n"
                "print(f'Adapter executed: {task}')\n"
            )
        ADAPTERS_DIR.mkdir(parents=True, exist_ok=True)
        (ADAPTERS_DIR / script_name).write_text(python_script, encoding="utf-8")

    manifest = {
        "name": name,
        "requirement": requirement,
        "description": description or f"Adapter for: {requirement or adapter_name}",
        "mode": mode,
        "command_template": command_template,
        "script_path": script_path,
        "input_schema": schema,
        "total_runs": 0,
        "success_runs": 0,
        "fail_runs": 0,
        "last_error": "",
    }
    path = _manifest_path(name)
    _save_manifest(path, manifest)

    return f"Adapter created: {manifest['name']} ({manifest['mode']}) at {path}"


def execute_adapter_tool(
    tool_name: str,
    tool_input: dict,
    run_command: Callable[[str, int], str],
) -> str | None:
    """Execute a dynamic adapter tool by name (adapter_<slug>)."""
    if not tool_name.startswith("adapter_"):
        return None

    key = tool_name[len("adapter_") :]
    manifest, path = _find_manifest_by_slug(key)
    if not manifest or not path:
        return f"[adapter missing] {tool_name}"

    mode = manifest.get("mode", "command")
    if mode == "command":
        template = manifest.get("command_template", "")
        cmd = _safe_format(template, tool_input)
        out = run_command(cmd, timeout=60)
        ok = "[error" not in (out or "").lower()
        manifest["total_runs"] = int(manifest.get("total_runs", 0)) + 1
        if ok:
            manifest["success_runs"] = int(manifest.get("success_runs", 0)) + 1
            manifest["last_error"] = ""
        else:
            manifest["fail_runs"] = int(manifest.get("fail_runs", 0)) + 1
            manifest["last_error"] = (out or "")[:240]
        _save_manifest(path, manifest)
        return out

    if mode == "python":
        script = manifest.get("script_path", "")
        if not script:
            return "[adapter error] missing script_path"
        payload = json.dumps(tool_input or {})
        payload_ps = payload.replace("'", "''")
        cmd = f"python \"{script}\" '{payload_ps}'"
        out = run_command(cmd, timeout=90)
        ok = (
            "[error" not in (out or "").lower()
            and "[adapter error]" not in (out or "").lower()
        )
        manifest["total_runs"] = int(manifest.get("total_runs", 0)) + 1
        if ok:
            manifest["success_runs"] = int(manifest.get("success_runs", 0)) + 1
            manifest["last_error"] = ""
        else:
            manifest["fail_runs"] = int(manifest.get("fail_runs", 0)) + 1
            manifest["last_error"] = (out or "")[:240]
        _save_manifest(path, manifest)
        return out

    return f"[adapter error] unsupported mode: {mode}"


def verify_local_adapter(
    adapter_name: str,
    sample_input_json: str,
    run_command: Callable[[str, int], str],
) -> str:
    """Run a smoke test for an adapter and persist health in manifest."""
    target = _slug(adapter_name)
    manifest = None
    path = None
    for p in sorted(ADAPTERS_DIR.glob("*.json")):
        m = _load_manifest(p)
        if not m:
            continue
        if _slug(m.get("name", "")) == target:
            manifest = m
            path = p
            break

    if not manifest or not path:
        return f"[verify] adapter not found: {adapter_name}"

    try:
        sample = json.loads(sample_input_json) if sample_input_json else {}
        if not isinstance(sample, dict):
            sample = {}
    except Exception:
        sample = {}

    tool_name = f"adapter_{target}"
    out = execute_adapter_tool(tool_name, sample, run_command)
    ok = (
        out is not None
        and ("[adapter error]" not in out.lower())
        and ("[error" not in out.lower())
    )

    manifest["last_verify_ts"] = int(time.time())
    manifest["last_verify_ok"] = bool(ok)
    manifest["last_verify_output"] = (out or "")[:800]
    _save_manifest(path, manifest)

    status = "PASS" if ok else "FAIL"
    return f"[verify {status}] {manifest.get('name', 'adapter')}\n{(out or '')[:900]}"
