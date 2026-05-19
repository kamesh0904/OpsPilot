"""
app/scheduler/jobs.py
──────────────────────
APScheduler cron jobs for OpsPilot's three scheduled briefings.

Three jobs run on a fixed cron schedule:
  morning_briefing_job  — every day at 09:00   (DAILY_MORNING)
  eod_pulse_job         — every day at 18:00   (EOD_PULSE)
  weekly_digest_job     — every Sunday at 18:00 (WEEKLY_DIGEST)

Lifecycle — how this plugs into FastAPI:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging()
        start_scheduler()       ← starts APScheduler, registers all jobs
        yield                   ← app serves requests here
        stop_scheduler()        ← shuts APScheduler down cleanly

The scheduler runs inside the same asyncio event loop as FastAPI.
APScheduler's AsyncIOScheduler dispatches each job as a coroutine on
that loop, so the jobs share the loop with HTTP requests — no threads
needed, no new event loop created.

Phase 1 constraints (Cloud Run, single worker):
  - APScheduler in-process is acceptable for Phase 1 because there is
    exactly one worker process. With multiple workers, every worker
    would fire the jobs independently and the founder would receive
    duplicate briefings. min-instances=1 on Cloud Run prevents cold
    starts that would cause scheduled jobs to be silently skipped.
  - If Phase 2 moves to multiple workers, replace APScheduler with
    a distributed scheduler (e.g. Celery Beat, Cloud Scheduler + Pub/Sub).
"""

from __future__ import annotations

import traceback
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.constants import BriefingType
from app.core.logging import get_logger
from app.graph import run_pipeline

log = get_logger(__name__)

# Module-level scheduler instance — created once, shared across start/stop calls.
# Exposed at module level so tests can introspect registered jobs.
_scheduler: Optional[AsyncIOScheduler] = None


# ── Job functions ─────────────────────────────────────────────────────────────
# Each function is an async coroutine — APScheduler's AsyncIOScheduler
# dispatches them on the running event loop.
#
# Error contract:
#   - A failed pipeline run must NEVER crash the scheduler process.
#   - Errors are caught, logged, and the scheduler continues.
#   - The next scheduled tick will still fire normally.
#   - Non-fatal run errors (one source down) are already surfaced inside
#     run_pipeline() via state["run_errors"] and logged there.

async def morning_briefing_job() -> None:
    """
    Daily 09:00 — Morning briefing.

    Full pipeline: all sources collected, PR reviews enriched,
    LLM cross-source pattern analysis, complete Slack Block Kit briefing.
    Output shape: URGENT items + WATCH items + one recommendation.
    """
    log.info("scheduler_job_start", job="morning_briefing")
    try:
        state = await run_pipeline(briefing_type=BriefingType.DAILY_MORNING)
        _log_job_result("morning_briefing", state)
    except Exception as exc:
        log.error(
            "scheduler_job_fatal",
            job="morning_briefing",
            error=str(exc),
            trace=traceback.format_exc(),
        )


async def eod_pulse_job() -> None:
    """
    Daily 18:00 — End-of-day pulse.

    Lighter collection: PR review enrichment skipped (run_pipeline
    auto-detects EOD_PULSE and sets enrich_reviews=False).
    Output shape: what moved today, what's at risk tomorrow.
    """
    log.info("scheduler_job_start", job="eod_pulse")
    try:
        state = await run_pipeline(briefing_type=BriefingType.EOD_PULSE)
        _log_job_result("eod_pulse", state)
    except Exception as exc:
        log.error(
            "scheduler_job_fatal",
            job="eod_pulse",
            error=str(exc),
            trace=traceback.format_exc(),
        )


async def weekly_digest_job() -> None:
    """
    Every Sunday 18:00 — Weekly digest.

    Full pipeline with LLM trend analysis.
    Output shape: velocity trends, recurring blockers, week-over-week patterns.
    """
    log.info("scheduler_job_start", job="weekly_digest")
    try:
        state = await run_pipeline(briefing_type=BriefingType.WEEKLY_DIGEST)
        _log_job_result("weekly_digest", state)
    except Exception as exc:
        log.error(
            "scheduler_job_fatal",
            job="weekly_digest",
            error=str(exc),
            trace=traceback.format_exc(),
        )


def _log_job_result(job_name: str, state: dict) -> None:
    """Emit a structured log summarising the outcome of a completed job."""
    action_result = state.get("action_result")
    run_errors = state.get("run_errors", [])

    log.info(
        "scheduler_job_done",
        job=job_name,
        briefing_sent=action_result.briefing_sent if action_result else False,
        run_errors=len(run_errors),
        # Surface the first error message if any, for quick visibility in logs
        first_error=run_errors[0] if run_errors else None,
    )


# ── Scheduler lifecycle ───────────────────────────────────────────────────────

def start_scheduler() -> AsyncIOScheduler:
    """
    Create, configure, and start the APScheduler AsyncIOScheduler.

    Cron expressions are read from settings so they can be overridden
    via environment variables without a code change. Defaults:
        daily_briefing_cron = "0 9 * * *"    (09:00 every day)
        eod_pulse_cron      = "0 18 * * *"   (18:00 every day)
        weekly_digest_cron  = "0 18 * * 0"   (18:00 every Sunday)

    Returns the started scheduler (stored in module-level _scheduler).
    Called once from FastAPI's lifespan on server startup.
    """
    global _scheduler

    _scheduler = AsyncIOScheduler()

    # Morning briefing — daily 09:00
    _scheduler.add_job(
        morning_briefing_job,
        trigger=CronTrigger.from_crontab(settings.daily_briefing_cron),
        id="morning_briefing",
        name="Daily Morning Briefing",
        replace_existing=True,   # safe to call start_scheduler() more than once
        max_instances=1,         # prevent overlapping runs if a job is slow
        misfire_grace_time=300,  # tolerate up to 5-min late fire (e.g. cold start)
    )

    # EOD pulse — daily 18:00
    _scheduler.add_job(
        eod_pulse_job,
        trigger=CronTrigger.from_crontab(settings.eod_pulse_cron),
        id="eod_pulse",
        name="End-of-Day Pulse",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )

    # Weekly digest — every Sunday 18:00
    _scheduler.add_job(
        weekly_digest_job,
        trigger=CronTrigger.from_crontab(settings.weekly_digest_cron),
        id="weekly_digest",
        name="Weekly Digest",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )

    _scheduler.start()

    log.info(
        "scheduler_started",
        jobs=[job.id for job in _scheduler.get_jobs()],
        morning_cron=settings.daily_briefing_cron,
        eod_cron=settings.eod_pulse_cron,
        weekly_cron=settings.weekly_digest_cron,
    )

    return _scheduler


def stop_scheduler() -> None:
    """
    Gracefully shut down the scheduler.

    Called from FastAPI's lifespan on server shutdown (the code after
    `yield` in the asynccontextmanager). Waits for any currently-running
    job to finish before returning (wait=True is the default).
    """
    global _scheduler

    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=True)
        log.info("scheduler_stopped")
        _scheduler = None


def get_scheduler() -> Optional[AsyncIOScheduler]:
    """
    Return the current scheduler instance, or None if not started.
    Used by API routes that need to inspect or manually trigger jobs.
    """
    return _scheduler
