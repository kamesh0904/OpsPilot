"""
app/api/routes/query.py
────────────────────────
POST /query

On-demand question answering via the LangGraph ON_DEMAND pipeline path.

The on-demand path is architecturally separate from scheduled briefings:
  - Runs the same four agents but with briefing_type=ON_DEMAND
  - The Analyst builds a focused prompt around the specific question
  - The Decision agent skips the dedup store (nothing is "seen" on demand)
  - The Action agent formats a conversational reply, not a full briefing

This route runs the pipeline synchronously (not background) because
the caller is waiting for an answer — unlike /briefing/trigger where
the result goes to Slack and the caller doesn't need to wait.

Target: < 15 seconds end-to-end.
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.constants import BriefingType
from app.core.logging import get_logger
from app.graph import run_pipeline

log = get_logger(__name__)
router = APIRouter()


# ── Request / Response models ─────────────────────────────────────────────────

class QueryRequest(BaseModel):
    """Body for POST /query."""
    question: str = Field(
        ...,
        min_length=3,
        max_length=500,
        description="The question to answer.",
        examples=["What's blocking the payment feature?"],
    )


class QueryResponse(BaseModel):
    """Response for POST /query."""
    run_id: str
    question: str
    answer: str = Field(description="Direct answer extracted from pipeline output.")
    sources_used: list[str] = Field(
        default_factory=list,
        description="Which integrations contributed data to this answer.",
    )
    run_errors: list[str] = Field(
        default_factory=list,
        description="Non-fatal errors during the run (e.g. one source unavailable).",
    )


# ── Route ─────────────────────────────────────────────────────────────────────

@router.post(
    "/query",
    response_model=QueryResponse,
    summary="Ask OpsPilot a question",
    tags=["Query"],
)
async def query(body: QueryRequest) -> QueryResponse:
    """
    Ask OpsPilot a specific question and receive a direct answer.

    Runs the full pipeline with `run_type=on_demand`. The Analyst receives
    the question and focuses its analysis on answering it rather than
    producing a full briefing.

    **Example questions:**
    - `"What's blocking the payment feature?"`
    - `"Which PRs have been open the longest?"`
    - `"Who owns the auth refactor ticket?"`

    Unlike `/briefing/trigger`, this endpoint **waits** for the pipeline
    to complete and returns the answer directly. Target latency: < 15s.
    """
    run_id = str(uuid.uuid4())
    log.info("query_request_received", run_id=run_id, question=body.question[:80])

    try:
        state = await run_pipeline(briefing_type=BriefingType.ON_DEMAND)
    except Exception as exc:
        log.error("query_pipeline_fatal", run_id=run_id, error=str(exc))
        raise HTTPException(
            status_code=503,
            detail="Pipeline failed to run. Please try again shortly.",
        )

    # Extract the answer from the decision output
    decision = state.get("decision")
    run_errors = state.get("run_errors", [])
    snapshot = state.get("snapshot")

    # Build the answer from decision output
    # On-demand: the briefing text is the direct answer
    answer_parts: list[str] = []
    if decision:
        if decision.urgent_items:
            answer_parts.extend(decision.urgent_items)
        if decision.watch_items:
            answer_parts.extend(decision.watch_items)
        if decision.suggestion:
            answer_parts.append(f"💡 {decision.suggestion}")

    answer = (
        "\n\n".join(answer_parts)
        if answer_parts
        else "No issues found related to your question."
    )

    # Determine which sources were actually used
    sources_used: list[str] = []
    if snapshot:
        if snapshot.linear_tickets:
            sources_used.append("linear")
        if snapshot.pull_requests:
            sources_used.append("github")
        if snapshot.notion_pages:
            sources_used.append("notion")

    log.info(
        "query_response_ready",
        run_id=run_id,
        sources=sources_used,
        run_errors=len(run_errors),
    )

    return QueryResponse(
        run_id=run_id,
        question=body.question,
        answer=answer,
        sources_used=sources_used,
        run_errors=run_errors,
    )
