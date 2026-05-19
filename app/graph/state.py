"""
app/graph/state.py
──────────────────
LangGraph state schema and node-function wrappers for the OpsPilot pipeline.

LangGraph requires node functions with a specific signature:
    async def node_fn(state: dict) -> dict

This file:
  1. Defines OpsState — the TypedDict that LangGraph uses as its graph state.
     (AgentState from agents/models.py is the Pydantic twin; OpsState is what
     LangGraph's StateGraph actually works with internally.)
  2. Provides four async node functions — one per agent — each with the
     correct LangGraph interface (receive dict, return dict of updates).
  3. Handles errors at the node boundary so a single failing node never
     crashes the graph silently.

Why a separate state.py instead of putting this in pipeline.py?
  - pipeline.py is concerned with graph wiring (add_node, add_edge, compile).
  - state.py is concerned with what data exists and how it's transformed.
  - Keeping them separate makes it easy to test node functions independently
    without constructing the full graph.
"""

from __future__ import annotations

import traceback
from datetime import datetime, timezone
from typing import Any, Optional
from typing_extensions import TypedDict

from app.core.constants import BriefingType
from app.core.logging import get_logger
from app.agents.collector import CollectorAgent
from app.agents.analyst import AnalystAgent
from app.agents.decision import DecisionAgent
from app.agents.action import ActionAgent

log = get_logger(__name__)


# ── LangGraph state schema ────────────────────────────────────────────────────

class OpsState(TypedDict, total=False):
    """
    The state object that flows through every node in the LangGraph pipeline.

    LangGraph updates state incrementally: each node returns a dict of *only*
    the keys it modified. LangGraph merges these updates into the running state.
    All fields are therefore Optional so that early nodes can return partial
    updates without specifying every key.

    Field lifecycle:
        briefing_type   — set by caller before the graph runs; read-only in nodes
        enrich_reviews  — set by caller; controls PR review API calls
        snapshot        — written by collector_node, read by analyst_node
        report          — written by analyst_node, read by decision_node
        decision        — written by decision_node, read by action_node
        action_result   — written by action_node; final output
        run_errors      — accumulated by any node that catches a non-fatal error
        started_at      — recorded at graph entry; used for run-duration logging
    """

    briefing_type: str              # "daily_morning" | "eod_pulse" | "weekly_digest" | "on_demand"
    enrich_reviews: bool            # True = fetch PR review decisions (slower, richer)
    snapshot: Optional[Any]         # CollectorSnapshot — typed as Any to avoid circular import
    report: Optional[Any]           # AnalystReport
    decision: Optional[Any]         # DecisionOutput
    action_result: Optional[Any]    # ActionResult
    run_errors: list[str]           # Non-fatal errors accumulated across nodes
    started_at: Optional[str]       # ISO timestamp string (set at graph entry)


def make_initial_state(
    briefing_type: str = BriefingType.DAILY_MORNING,
    enrich_reviews: bool = True,
) -> OpsState:
    """
    Build the initial OpsState before graph execution begins.

    Args:
        briefing_type:   What kind of run this is (morning/eod/weekly/ondemand).
        enrich_reviews:  Whether the GitHub client should fetch review decisions.
                         Set to False for EOD pulse to save API quota.

    Returns:
        A fully initialised OpsState dict ready to hand to graph.ainvoke().
    """
    return OpsState(
        briefing_type=briefing_type,
        enrich_reviews=enrich_reviews,
        snapshot=None,
        report=None,
        decision=None,
        action_result=None,
        run_errors=[],
        started_at=datetime.now(timezone.utc).isoformat(),
    )


# ── Node functions ────────────────────────────────────────────────────────────
# Each function receives the full current state dict and returns a dict of
# only the keys it wants to update.  LangGraph merges the return value into
# the running state before passing it to the next node.
#
# Error contract:
#   - Non-fatal errors (one source down, Slack hiccup) are appended to
#     run_errors and the node returns partial state so the graph continues.
#   - Fatal errors (unrecoverable) are re-raised; LangGraph marks the run
#     as failed and the caller's try/except handles alerting.

async def collector_node(state: OpsState) -> dict:
    """
    Node 1 — Collector Agent.
    Fetches data from Linear, GitHub, and Notion in parallel.
    Always returns a CollectorSnapshot (may be partial if a source fails).
    """
    briefing_type = state.get("briefing_type", BriefingType.DAILY_MORNING)
    enrich_reviews = state.get("enrich_reviews", True)

    # EOD pulse: skip PR review enrichment to save API calls (~10s faster)
    if briefing_type == BriefingType.EOD_PULSE:
        enrich_reviews = False

    log.info("graph_collector_node_start", briefing_type=briefing_type)

    try:
        agent = CollectorAgent()
        snapshot = await agent.run(
            briefing_type=briefing_type,
            enrich_pr_reviews=enrich_reviews,
        )

        # Surface any per-source errors into the graph's run_errors list
        new_errors: list[str] = list(state.get("run_errors", []))
        for source, err_msg in snapshot.errors.items():
            new_errors.append(f"[collector] {source}: {err_msg}")

        log.info(
            "graph_collector_node_done",
            tickets=len(snapshot.linear_tickets),
            prs=len(snapshot.pull_requests),
            pages=len(snapshot.notion_pages),
            source_errors=len(snapshot.errors),
        )

        return {"snapshot": snapshot, "run_errors": new_errors}

    except Exception as exc:
        # Fatal — can't proceed without any data at all
        log.error("graph_collector_node_fatal", error=str(exc), trace=traceback.format_exc())
        raise


async def analyst_node(state: OpsState) -> dict:
    """
    Node 2 — Analyst Agent.
    Reads snapshot, runs rule-based + optional LLM analysis.
    Falls back gracefully if Gemini is unavailable.
    """
    snapshot = state.get("snapshot")
    if snapshot is None:
        # Collector must have failed fatally — nothing to analyse
        log.error("graph_analyst_node_skipped", reason="no snapshot in state")
        raise RuntimeError("Analyst node received no snapshot — collector may have failed")

    briefing_type = state.get("briefing_type", BriefingType.DAILY_MORNING)
    log.info("graph_analyst_node_start", briefing_type=briefing_type)

    try:
        agent = AnalystAgent(use_llm=True)
        report = await agent.run(snapshot)

        log.info(
            "graph_analyst_node_done",
            urgent=report.urgent_count,
            watch=report.watch_count,
            llm_used=report.llm_used,
        )

        return {"report": report}

    except Exception as exc:
        # Non-fatal: surface error and continue with an empty report
        # The Decision and Action nodes handle empty reports gracefully
        log.error("graph_analyst_node_error", error=str(exc), trace=traceback.format_exc())
        errors = list(state.get("run_errors", []))
        errors.append(f"[analyst] {exc}")

        from app.agents.models import AnalystReport
        return {"report": AnalystReport(), "run_errors": errors}


async def decision_node(state: OpsState) -> dict:
    """
    Node 3 — Decision Agent.
    Reads the AnalystReport and applies priority logic + deduplication.
    Returns a DecisionOutput ready for the Action agent.
    """
    report = state.get("report")
    if report is None:
        log.error("graph_decision_node_skipped", reason="no report in state")
        raise RuntimeError("Decision node received no report — analyst may have failed fatally")

    briefing_type = state.get("briefing_type", BriefingType.DAILY_MORNING)
    log.info(
        "graph_decision_node_start",
        briefing_type=briefing_type,
        findings=len(report.findings),
    )

    try:
        agent = DecisionAgent()
        decision = await agent.run(report, briefing_type=briefing_type)

        log.info(
            "graph_decision_node_done",
            urgent=len(decision.urgent_items),
            watch=len(decision.watch_items),
            shipped=len(decision.shipped_items),
            is_empty=decision.is_empty,
        )

        return {"decision": decision}

    except Exception as exc:
        log.error("graph_decision_node_error", error=str(exc), trace=traceback.format_exc())
        errors = list(state.get("run_errors", []))
        errors.append(f"[decision] {exc}")

        from app.agents.models import DecisionOutput
        return {"decision": DecisionOutput(), "run_errors": errors}


async def action_node(state: OpsState) -> dict:
    """
    Node 4 — Action Agent.
    Reads DecisionOutput and sends the Slack briefing.
    Optionally creates Linear tickets / updates Notion pages.
    """
    decision = state.get("decision")
    if decision is None:
        log.error("graph_action_node_skipped", reason="no decision in state")
        raise RuntimeError("Action node received no decision — decision agent may have failed fatally")

    briefing_type = state.get("briefing_type", BriefingType.DAILY_MORNING)
    log.info(
        "graph_action_node_start",
        briefing_type=briefing_type,
        is_empty=decision.is_empty,
    )

    errors = list(state.get("run_errors", []))

    try:
        agent = ActionAgent(
            auto_create_tickets=False,   # opt-in only — safe default
            auto_update_notion=False,
        )
        result = await agent.run(decision, briefing_type=briefing_type)

        # Merge any errors the Action agent encountered into run_errors
        for err in result.errors:
            errors.append(f"[action] {err}")

        log.info(
            "graph_action_node_done",
            briefing_sent=result.briefing_sent,
            tickets_created=len(result.tickets_created),
            notion_updated=len(result.notion_pages_updated),
            action_errors=len(result.errors),
        )

        return {"action_result": result, "run_errors": errors}

    except Exception as exc:
        log.error("graph_action_node_error", error=str(exc), trace=traceback.format_exc())
        errors.append(f"[action] {exc}")

        from app.agents.models import ActionResult
        return {
            "action_result": ActionResult(briefing_sent=False, errors=[str(exc)]),
            "run_errors": errors,
        }
