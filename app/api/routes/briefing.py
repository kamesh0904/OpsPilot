"""
app/api/routes/briefing.py
───────────────────────────
POST /briefing/trigger

Manually triggers the full LangGraph pipeline for a given briefing type.
Used by:
  - The founder hitting "Run now" in a future dashboard
  - Internal testing / debugging without waiting for the cron
  - The Slack /opspilot brief command

The pipeline runs in the background (BackgroundTasks) so the HTTP response
returns immediately with a run_id. The briefing is delivered to Slack by
the Action agent, not returned in the HTTP response body.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from app.core.constants import BriefingType
from app.core.logging import get_logger
from app.graph import run_pipeline

log = get_logger(__name__)
router = APIRouter()


# ── Request / Response models ─────────────────────────────────────────────────

class TriggerRequest(BaseModel):
    """Body for POST /briefing/trigger."""
    run_type: Literal["morning", "evening", "weekly"] = Field(
        ...,
        description="Which briefing type to run.",
        examples=["morning"],
    )

    @property
    def briefing_type(self) -> str:
        """Map the API's short name to the internal BriefingType constant."""
        return {
            "morning": BriefingType.DAILY_MORNING,
            "evening": BriefingType.EOD_PULSE,
            "weekly":  BriefingType.WEEKLY_DIGEST,
        }[self.run_type]


class TriggerResponse(BaseModel):
    """Response for POST /briefing/trigger."""
    run_id: str = Field(description="Unique ID for this pipeline run (for audit/tracing).")
    status: Literal["started"] = "started"
    briefing_type: str = Field(description="The resolved internal briefing type.")
    message: str = Field(description="Human-readable status message.")


# ── Background task ───────────────────────────────────────────────────────────

async def _run_pipeline_task(run_id: str, briefing_type: str) -> None:
    """
    Background coroutine that runs the pipeline and logs the outcome.
    Errors are caught and logged — they must not crash the background task
    runner, as that would kill the server process.
    """
    log.info("briefing_trigger_pipeline_start", run_id=run_id, briefing_type=briefing_type)
    try:
        state = await run_pipeline(briefing_type=briefing_type)
        action_result = state.get("action_result")
        run_errors = state.get("run_errors", [])
        log.info(
            "briefing_trigger_pipeline_done",
            run_id=run_id,
            briefing_sent=action_result.briefing_sent if action_result else False,
            run_errors=len(run_errors),
        )
    except Exception as exc:
        log.error("briefing_trigger_pipeline_fatal", run_id=run_id, error=str(exc))


# ── Route ─────────────────────────────────────────────────────────────────────

@router.post(
    "/briefing/trigger",
    response_model=TriggerResponse,
    summary="Manually trigger a briefing run",
    tags=["Briefing"],
)
async def trigger_briefing(
    body: TriggerRequest,
    background_tasks: BackgroundTasks,
) -> TriggerResponse:
    """
    Trigger the full OpsPilot pipeline immediately.

    The pipeline runs in the background — this endpoint returns as soon as
    the background task is registered (< 50ms). The actual briefing is
    delivered to Slack by the Action agent when the pipeline completes.

    **run_type values:**
    - `morning` → Full briefing with URGENT + WATCH + recommendation
    - `evening` → EOD pulse (lighter, no PR review enrichment)
    - `weekly`  → Full trend analysis and velocity digest
    """
    run_id = str(uuid.uuid4())

    background_tasks.add_task(
        _run_pipeline_task,
        run_id=run_id,
        briefing_type=body.briefing_type,
    )

    log.info(
        "briefing_trigger_accepted",
        run_id=run_id,
        run_type=body.run_type,
        briefing_type=body.briefing_type,
    )

    return TriggerResponse(
        run_id=run_id,
        status="started",
        briefing_type=body.briefing_type,
        message=f"{body.run_type.capitalize()} briefing started. Check Slack shortly.",
    )
