"""
Scheduled actions using APScheduler.

Enables time-based triggers for tasks.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Callable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config

log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()
_scheduled_jobs = {}


def init_scheduler():
    """Initialize the scheduler."""
    try:
        scheduler.start()
        log.info("Scheduler started")
    except Exception as e:
        log.error(f"Scheduler init failed: {e}")


def shutdown_scheduler():
    """Shutdown the scheduler."""
    try:
        scheduler.shutdown()
        log.info("Scheduler stopped")
    except Exception as e:
        log.warning(f"Scheduler shutdown error: {e}")


async def schedule_interval(
    job_id: str,
    callback: Callable,
    seconds: int = None,
    minutes: int = None,
    hours: int = None,
    **callback_args,
) -> str:
    """Schedule a job to run at intervals."""
    if seconds:
        trigger = IntervalTrigger(seconds=seconds)
    elif minutes:
        trigger = IntervalTrigger(minutes=minutes)
    elif hours:
        trigger = IntervalTrigger(hours=hours)
    else:
        return "[error] Must specify seconds, minutes, or hours"

    job = scheduler.add_job(
        callback,
        trigger,
        id=job_id,
        replace_existing=True,
        **callback_args,
    )

    _scheduled_jobs[job_id] = job
    log.info(f"Scheduled interval job: {job_id}")
    return f"[scheduled] {job_id} every {seconds or minutes or hours}"


async def schedule_cron(
    job_id: str,
    callback: Callable,
    cron: str,
    **callback_args,
) -> str:
    """Schedule a job with cron expression."""
    # Parse simple cron: "hour.minute" or "hour.minute.day_of_week"
    parts = cron.split(".")

    kwargs = {}
    if len(parts) >= 1:
        kwargs["minute"] = parts[0] if "*" not in parts[0] else None
    if len(parts) >= 2:
        kwargs["hour"] = parts[1] if "*" not in parts[1] else None
    if len(parts) >= 3:
        kwargs["day_of_week"] = parts[2] if "*" not in parts[2] else None

    # Remove None values
    kwargs = {k: v for k, v in kwargs.items() if v is not None}

    if not kwargs:
        return "[error] Invalid cron expression"

    trigger = CronTrigger(**kwargs)

    job = scheduler.add_job(
        callback,
        trigger,
        id=job_id,
        replace_existing=True,
        **callback_args,
    )

    _scheduled_jobs[job_id] = job
    log.info(f"Scheduled cron job: {job_id}")
    return f"[scheduled] {job_id} cron: {cron}"


async def schedule_once(
    job_id: str,
    callback: Callable,
    run_date: datetime,
    **callback_args,
) -> str:
    """Schedule a one-time job."""
    from apscheduler.triggers.date import DateTrigger

    job = scheduler.add_job(
        callback,
        DateTrigger(run_date=run_date),
        id=job_id,
        replace_existing=True,
        **callback_args,
    )

    _scheduled_jobs[job_id] = job
    log.info(f"Scheduled one-time job: {job_id} at {run_date}")
    return f"[scheduled] {job_id} at {run_date}"


async def unschedule(job_id: str) -> str:
    """Remove a scheduled job."""
    try:
        job = scheduler.remove_job(job_id)
        _scheduled_jobs.pop(job_id, None)
        return f"[unscheduled] {job_id}"
    except Exception as e:
        return f"[error] {e}"


async def list_jobs() -> str:
    """List all scheduled jobs."""
    jobs = scheduler.get_jobs()

    if not jobs:
        return "No scheduled jobs."

    output = ["## Scheduled Jobs\n"]
    for job in jobs:
        next_run = (
            job.next_run_time.strftime("%Y-%m-%d %H:%M") if job.next_run_time else "N/A"
        )
        output.append(f"- {job.id}: next {next_run}")

    return "\n".join(output)
