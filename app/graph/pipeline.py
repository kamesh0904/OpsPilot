"""
app/graph/pipeline.py
──────────────────────
LangGraph pipeline construction and public run interface.

This file does two things:
  1. Builds and compiles the StateGraph — the directed graph of agents.
  2. Exposes run_pipeline() — the single public function called by both
     the scheduler (Step 5) and the API routes.

Graph topology (scheduled briefing path):
    collector_node
         │
         ▼
    analyst_node
         │
         ▼
    decision_node
         │
         ▼
    action_node
         │
         ▼
      END

Why compile() once at module load?
  - Compilation validates the graph structure (no orphaned nodes, no missing
    edges) at startup rather than at runtime.
  - The compiled graph object is reusable — each run_pipeline() call invokes
    the same compiled graph with a fresh initial state.
  - Avoids re-building the graph on every scheduler tick.

Thread safety / async:
  - The graph is compiled once and shared. LangGraph's CompiledGraph is
    async-safe — concurrent invocations each get their own state copy.
  - FastAPI + APScheduler both run in the same asyncio event loop (single
    worker in Phase 1), so concurrent runs are rare but safe.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from langgraph.graph import StateGraph, END

from app.core.constants import BriefingType
from app.core.logging import get_logger
from app.graph.state import (
    OpsState,
    make_initial_state,
    collector_node,
    analyst_node,
    decision_node,
    action_node,
)

log = get_logger(__name__)


# ── Graph construction ────────────────────────────────────────────────────────

def _build_graph() -> StateGraph:
    """
    Construct and compile the four-node LangGraph pipeline.

    Node names match the AgentName enum values so logs and traces are
    consistent with agent-level logging.

    Returns a compiled graph ready to call .ainvoke() on.
    """
    graph = StateGraph(OpsState)

    # Register nodes — names are what appear in LangGraph traces
    graph.add_node("collector", collector_node)
    graph.add_node("analyst",   analyst_node)
    graph.add_node("decision",  decision_node)
    graph.add_node("action",    action_node)

    # Wire the linear pipeline: collector → analyst → decision → action → END
    graph.set_entry_point("collector")
    graph.add_edge("collector", "analyst")
    graph.add_edge("analyst",   "decision")
    graph.add_edge("decision",  "action")
    graph.add_edge("action",    END)

    return graph.compile()


# Compiled graph — built once at import time, reused on every run.
_pipeline = _build_graph()


# ── Public interface ──────────────────────────────────────────────────────────

async def run_pipeline(
    briefing_type: str = BriefingType.DAILY_MORNING,
    enrich_reviews: Optional[bool] = None,
) -> OpsState:
    """
    Run the full four-agent pipeline and return the final OpsState.

    This is the single entry point for:
      - APScheduler cron jobs (Step 5 / scheduler/jobs.py)
      - The FastAPI POST /briefing/trigger route
      - Tests

    Args:
        briefing_type:
            What kind of briefing to produce.
            One of: "daily_morning" | "eod_pulse" | "weekly_digest" | "on_demand"

        enrich_reviews:
            Whether the GitHub collector should fetch per-PR review decisions.
            Defaults to True for morning/weekly runs, False for eod_pulse
            (saves ~10s of API calls). Pass explicitly to override.

    Returns:
        The final OpsState after all four agents have run.
        Inspect state["action_result"] for the outcome.
        Inspect state["run_errors"] for any non-fatal errors.

    Raises:
        RuntimeError: if a fatal error occurs in the collector or if the
                      graph topology is violated (should not happen in normal use).

    Example::

        state = await run_pipeline(briefing_type=BriefingType.DAILY_MORNING)
        if state["action_result"].briefing_sent:
            log.info("briefing delivered")
        if state["run_errors"]:
            log.warning("partial run", errors=state["run_errors"])
    """
    # Default review enrichment based on briefing type
    if enrich_reviews is None:
        enrich_reviews = briefing_type != BriefingType.EOD_PULSE

    initial_state = make_initial_state(
        briefing_type=briefing_type,
        enrich_reviews=enrich_reviews,
    )

    log.info(
        "pipeline_start",
        briefing_type=briefing_type,
        enrich_reviews=enrich_reviews,
    )

    start = datetime.now(timezone.utc)

    try:
        final_state: OpsState = await _pipeline.ainvoke(initial_state)

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        action_result = final_state.get("action_result")
        run_errors = final_state.get("run_errors", [])

        log.info(
            "pipeline_done",
            briefing_type=briefing_type,
            elapsed_seconds=round(elapsed, 2),
            briefing_sent=action_result.briefing_sent if action_result else False,
            run_errors=len(run_errors),
        )

        return final_state

    except Exception as exc:
        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        log.error(
            "pipeline_fatal",
            briefing_type=briefing_type,
            elapsed_seconds=round(elapsed, 2),
            error=str(exc),
        )
        raise
