"""
Process registry - background process management.

Manages long-running background tasks:
- Start processes in background
- Track status
- Get output
- Cancel processes
- Notification on completion
"""

import asyncio
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import config

log = logging.getLogger(__name__)


class ProcessStatus(Enum):
    """Status of a background process."""

    STARTING = "starting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class BackgroundProcess:
    """A managed background process."""

    process_id: str
    command: str
    status: ProcessStatus = ProcessStatus.STARTING
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    pid: Optional[int] = None
    notify_on_complete: bool = True
    notification_callback: Optional[Callable] = None


class ProcessRegistry:
    """
    Manages background processes.

    Features:
    - Start processes in background
    - Track status and output
    - Cancel processes
    - Notify on completion
    """

    def __init__(self):
        self._processes: dict[str, BackgroundProcess] = {}
        self._lock = threading.Lock()
        self._watcher_thread: Optional[threading.Thread] = None
        self._running = False

    def start_process(
        self,
        command: str,
        process_id: str = None,
        notify_on_complete: bool = True,
        notification_callback: Callable = None,
        cwd: str = None,
        env: dict = None,
    ) -> str:
        """Start a process in the background."""
        if process_id is None:
            process_id = f"proc_{int(time.time() * 1000)}"

        # Create process object
        proc = BackgroundProcess(
            process_id=process_id,
            command=command,
            status=ProcessStatus.RUNNING,
            notify_on_complete=notify_on_complete,
            notification_callback=notification_callback,
        )

        with self._lock:
            self._processes[process_id] = proc

        # Start actual process in thread
        def _run():
            try:
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=None,  # No timeout for background
                    cwd=cwd or str(Path.home()),
                    env=env,
                )

                with self._lock:
                    proc = self._processes.get(process_id)
                    if proc:
                        proc.status = ProcessStatus.COMPLETED
                        proc.exit_code = result.returncode
                        proc.stdout = result.stdout
                        proc.stderr = result.stderr
                        proc.completed_at = time.time()

                # Notify
                if notify_on_complete and notification_callback:
                    try:
                        notification_callback(
                            process_id, result.returncode, result.stdout
                        )
                    except Exception as e:
                        log.warning(f"Notification callback failed: {e}")

            except Exception as e:
                with self._lock:
                    proc = self._processes.get(process_id)
                    if proc:
                        proc.status = ProcessStatus.FAILED
                        proc.stderr = str(e)
                        proc.completed_at = time.time()

                if notify_on_complete and notification_callback:
                    try:
                        notification_callback(process_id, -1, str(e))
                    except Exception:
                        pass

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        log.info(f"Started background process: {process_id}")

        return process_id

    def get_process(self, process_id: str) -> Optional[BackgroundProcess]:
        """Get process info."""
        with self._lock:
            return self._processes.get(process_id)

    def list_processes(self, status: ProcessStatus = None) -> list[BackgroundProcess]:
        """List all processes, optionally filtered by status."""
        with self._lock:
            procs = list(self._processes.values())

        if status:
            procs = [p for p in procs if p.status == status]

        return procs

    def cancel_process(self, process_id: str) -> bool:
        """Cancel a running process."""
        with self._lock:
            proc = self._processes.get(process_id)
            if not proc:
                return False

            if proc.status != ProcessStatus.RUNNING:
                return False

            proc.status = ProcessStatus.CANCELLED
            proc.completed_at = time.time()

        # Try to kill the actual process
        if proc.pid:
            try:
                import psutil

                p = psutil.Process(proc.pid)
                p.kill()
            except Exception:
                pass

        log.info(f"Cancelled process: {process_id}")
        return True

    def get_output(self, process_id: str) -> tuple[str, str]:
        """Get stdout/stderr of a process."""
        with self._lock:
            proc = self._processes.get(process_id)
            if not proc:
                return "", "Process not found"

        return proc.stdout, proc.stderr

    def wait_for(self, process_id: str, timeout: float = None) -> int:
        """Wait for process to complete, return exit code."""
        start = time.time()

        while True:
            with self._lock:
                proc = self._processes.get(process_id)
                if not proc:
                    return -1

                if proc.status in [
                    ProcessStatus.COMPLETED,
                    ProcessStatus.FAILED,
                    ProcessStatus.CANCELLED,
                ]:
                    return proc.exit_code or 0

            if timeout and (time.time() - start) > timeout:
                return -1

            time.sleep(0.5)

    def cleanup_completed(self, older_than_seconds: int = 3600):
        """Remove completed processes older than N seconds."""
        with self._lock:
            to_remove = []
            now = time.time()

            for pid, proc in self._processes.items():
                if proc.completed_at and (now - proc.completed_at) > older_than_seconds:
                    to_remove.append(pid)

            for pid in to_remove:
                del self._processes[pid]

            return len(to_remove)


# Global registry
_registry: Optional[ProcessRegistry] = None


def get_process_registry() -> ProcessRegistry:
    global _registry
    if _registry is None:
        _registry = ProcessRegistry()
    return _registry


# Convenience functions
async def run_background(
    command: str,
    process_id: str = None,
    notify_on_complete: bool = True,
    notification_callback: Callable = None,
) -> str:
    """Run a command in background."""
    return get_process_registry().start_process(
        command=command,
        process_id=process_id,
        notify_on_complete=notify_on_complete,
        notification_callback=notification_callback,
    )


def get_background_status(process_id: str) -> str:
    """Get status of a background process."""
    proc = get_process_registry().get_process(process_id)
    if not proc:
        return "not_found"
    return proc.status.value


def cancel_background(process_id: str) -> bool:
    """Cancel a background process."""
    return get_process_registry().cancel_process(process_id)
