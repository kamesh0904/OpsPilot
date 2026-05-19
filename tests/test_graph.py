"""
tests/test_graph.py
────────────────────
Unit tests for app/graph/ — LangGraph state schema and node functions.

Coverage:
  TestOpsState              (4 tests) — TypedDict structure, make_initial_state
  TestCollectorNode         (4 tests) — success path, source-error accumulation,
                                        EOD pulse disables review enrichment, fatal error
  TestAnalystNode           (4 tests) — success path, missing snapshot raises,
                                        LLM failure falls back gracefully
  TestDecisionNode          (4 tests) — success path, missing report raises,
                                        exception → empty DecisionOutput
  TestActionNode            (4 tests) — success path, missing decision raises,
                                        action errors accumulated in run_errors
  TestPipelineModule        (4 tests) — make_initial_state defaults, enrich_reviews
                                        defaulting logic, run_pipeline integration
                                        (fully mocked — no real API calls)

Total: 24 tests.
Zero real API calls. Zero LLM calls. All agents are patched.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.constants import BriefingType
from app.graph.state import (
    OpsState,
    make_initial_state,
    collector_node,
    analyst_node,
    decision_node,
    action_node,
)


# ── Shared fixtures ───────────────────────────────────────────────────────────

def _make_snapshot(errors: dict | None = None):
    """Return a minimal CollectorSnapshot-like object."""
    snap = MagicMock()
    snap.linear_tickets = []
    snap.pull_requests = []
    snap.notion_pages = []
    snap.errors = errors or {}
    return snap


def _make_report(urgent_count: int = 0, watch_count: int = 0):
    """Return a minimal AnalystReport-like object."""
    report = MagicMock()
    report.findings = []
    report.shipped_items = []
    report.patterns = []
    report.urgent_count = urgent_count
    report.watch_count = watch_count
    report.llm_used = False
    return report


def _make_decision(is_empty: bool = False):
    """Return a minimal DecisionOutput-like object."""
    decision = MagicMock()
    decision.urgent_items = [] if is_empty else ["🔴 *Stale PR*\n   ↳ 6 days open"]
    decision.watch_items = []
    decision.shipped_items = []
    decision.is_empty = is_empty
    return decision


def _make_action_result(briefing_sent: bool = True, errors: list | None = None):
    """Return a minimal ActionResult-like object."""
    result = MagicMock()
    result.briefing_sent = briefing_sent
    result.tickets_created = []
    result.notion_pages_updated = []
    result.errors = errors or []
    return result


# ── TestOpsState ──────────────────────────────────────────────────────────────

class TestOpsState:

    def test_make_initial_state_defaults(self):
        """make_initial_state() with no args produces sane defaults."""
        state = make_initial_state()
        assert state["briefing_type"] == BriefingType.DAILY_MORNING
        assert state["enrich_reviews"] is True
        assert state["snapshot"] is None
        assert state["report"] is None
        assert state["decision"] is None
        assert state["action_result"] is None
        assert state["run_errors"] == []

    def test_make_initial_state_custom_briefing_type(self):
        """Caller can specify a different briefing_type."""
        state = make_initial_state(briefing_type=BriefingType.EOD_PULSE)
        assert state["briefing_type"] == BriefingType.EOD_PULSE

    def test_make_initial_state_enrich_reviews_false(self):
        """enrich_reviews can be set to False explicitly."""
        state = make_initial_state(enrich_reviews=False)
        assert state["enrich_reviews"] is False

    def test_make_initial_state_has_started_at(self):
        """started_at is an ISO timestamp string."""
        state = make_initial_state()
        ts = state["started_at"]
        assert isinstance(ts, str)
        # Should parse as a valid ISO datetime
        parsed = datetime.fromisoformat(ts)
        assert parsed.tzinfo is not None   # timezone-aware


# ── TestCollectorNode ─────────────────────────────────────────────────────────

class TestCollectorNode:

    @pytest.mark.asyncio
    async def test_success_path(self):
        """collector_node returns snapshot and empty run_errors on success."""
        snapshot = _make_snapshot()
        state: OpsState = make_initial_state()

        with patch(
            "app.graph.state.CollectorAgent.run",
            new=AsyncMock(return_value=snapshot),
        ):
            result = await collector_node(state)

        assert result["snapshot"] is snapshot
        assert result["run_errors"] == []

    @pytest.mark.asyncio
    async def test_source_errors_propagate_to_run_errors(self):
        """Per-source failures inside the snapshot are added to run_errors."""
        snapshot = _make_snapshot(errors={"linear": "timeout", "github": "401"})
        state: OpsState = make_initial_state()

        with patch(
            "app.graph.state.CollectorAgent.run",
            new=AsyncMock(return_value=snapshot),
        ):
            result = await collector_node(state)

        errors = result["run_errors"]
        assert any("linear" in e for e in errors)
        assert any("github" in e for e in errors)

    @pytest.mark.asyncio
    async def test_eod_pulse_disables_review_enrichment(self):
        """EOD pulse runs pass enrich_pr_reviews=False to save API calls."""
        snapshot = _make_snapshot()
        state: OpsState = make_initial_state(briefing_type=BriefingType.EOD_PULSE)

        with patch("app.graph.state.CollectorAgent.run", new=AsyncMock(return_value=snapshot)) as mock_run:
            await collector_node(state)

        _, kwargs = mock_run.call_args
        assert kwargs.get("enrich_pr_reviews") is False

    @pytest.mark.asyncio
    async def test_fatal_error_propagates(self):
        """Fatal collector errors re-raise (no silent failure)."""
        state: OpsState = make_initial_state()

        with patch(
            "app.graph.state.CollectorAgent.run",
            new=AsyncMock(side_effect=RuntimeError("network dead")),
        ):
            with pytest.raises(RuntimeError, match="network dead"):
                await collector_node(state)


# ── TestAnalystNode ───────────────────────────────────────────────────────────

class TestAnalystNode:

    @pytest.mark.asyncio
    async def test_success_path(self):
        """analyst_node returns report when snapshot is present."""
        report = _make_report(urgent_count=2)
        snapshot = _make_snapshot()
        state: OpsState = {**make_initial_state(), "snapshot": snapshot}

        with patch(
            "app.graph.state.AnalystAgent.run",
            new=AsyncMock(return_value=report),
        ):
            result = await analyst_node(state)

        assert result["report"] is report

    @pytest.mark.asyncio
    async def test_missing_snapshot_raises(self):
        """analyst_node raises RuntimeError if no snapshot in state."""
        state: OpsState = make_initial_state()  # snapshot is None

        with pytest.raises(RuntimeError, match="no snapshot"):
            await analyst_node(state)

    @pytest.mark.asyncio
    async def test_llm_failure_returns_empty_report(self):
        """If AnalystAgent.run raises, node returns empty AnalystReport and logs error."""
        snapshot = _make_snapshot()
        state: OpsState = {**make_initial_state(), "snapshot": snapshot}

        with patch(
            "app.graph.state.AnalystAgent.run",
            new=AsyncMock(side_effect=Exception("gemini down")),
        ):
            result = await analyst_node(state)

        # Should return an empty (but valid) report, not crash
        assert result["report"] is not None
        assert "analyst" in result["run_errors"][0]

    @pytest.mark.asyncio
    async def test_error_accumulates_in_run_errors(self):
        """Analyst errors are appended, not overwriting, existing run_errors."""
        snapshot = _make_snapshot()
        state: OpsState = {
            **make_initial_state(),
            "snapshot": snapshot,
            "run_errors": ["[collector] linear: timeout"],
        }

        with patch(
            "app.graph.state.AnalystAgent.run",
            new=AsyncMock(side_effect=Exception("boom")),
        ):
            result = await analyst_node(state)

        assert len(result["run_errors"]) == 2
        assert "[collector] linear: timeout" in result["run_errors"]


# ── TestDecisionNode ──────────────────────────────────────────────────────────

class TestDecisionNode:

    @pytest.mark.asyncio
    async def test_success_path(self):
        """decision_node returns decision when report is present."""
        report = _make_report()
        decision = _make_decision()
        state: OpsState = {**make_initial_state(), "report": report}

        with patch(
            "app.graph.state.DecisionAgent.run",
            new=AsyncMock(return_value=decision),
        ):
            result = await decision_node(state)

        assert result["decision"] is decision

    @pytest.mark.asyncio
    async def test_missing_report_raises(self):
        """decision_node raises RuntimeError if no report in state."""
        state: OpsState = make_initial_state()  # report is None

        with pytest.raises(RuntimeError, match="no report"):
            await decision_node(state)

    @pytest.mark.asyncio
    async def test_exception_returns_empty_decision(self):
        """If DecisionAgent.run raises, node returns empty DecisionOutput."""
        report = _make_report()
        state: OpsState = {**make_initial_state(), "report": report}

        with patch(
            "app.graph.state.DecisionAgent.run",
            new=AsyncMock(side_effect=Exception("dedup crash")),
        ):
            result = await decision_node(state)

        assert result["decision"] is not None
        assert "decision" in result["run_errors"][0]

    @pytest.mark.asyncio
    async def test_briefing_type_forwarded_to_agent(self):
        """decision_node passes the correct briefing_type to DecisionAgent.run."""
        report = _make_report()
        decision = _make_decision()
        state: OpsState = {
            **make_initial_state(briefing_type=BriefingType.WEEKLY_DIGEST),
            "report": report,
        }

        with patch(
            "app.graph.state.DecisionAgent.run",
            new=AsyncMock(return_value=decision),
        ) as mock_run:
            await decision_node(state)

        _, kwargs = mock_run.call_args
        assert kwargs.get("briefing_type") == BriefingType.WEEKLY_DIGEST


# ── TestActionNode ────────────────────────────────────────────────────────────

class TestActionNode:

    @pytest.mark.asyncio
    async def test_success_path(self):
        """action_node returns action_result when decision is present."""
        decision = _make_decision()
        action_result = _make_action_result(briefing_sent=True)
        state: OpsState = {**make_initial_state(), "decision": decision}

        with patch(
            "app.graph.state.ActionAgent.run",
            new=AsyncMock(return_value=action_result),
        ):
            result = await action_node(state)

        assert result["action_result"] is action_result

    @pytest.mark.asyncio
    async def test_missing_decision_raises(self):
        """action_node raises RuntimeError if no decision in state."""
        state: OpsState = make_initial_state()  # decision is None

        with pytest.raises(RuntimeError, match="no decision"):
            await action_node(state)

    @pytest.mark.asyncio
    async def test_action_errors_merged_into_run_errors(self):
        """Errors inside ActionResult.errors are added to state run_errors."""
        decision = _make_decision()
        action_result = _make_action_result(
            briefing_sent=False,
            errors=["Failed to send Slack briefing"],
        )
        state: OpsState = {**make_initial_state(), "decision": decision}

        with patch(
            "app.graph.state.ActionAgent.run",
            new=AsyncMock(return_value=action_result),
        ):
            result = await action_node(state)

        assert any("Slack" in e for e in result["run_errors"])

    @pytest.mark.asyncio
    async def test_exception_returns_failed_action_result(self):
        """If ActionAgent.run raises, node returns a failed ActionResult."""
        decision = _make_decision()
        state: OpsState = {**make_initial_state(), "decision": decision}

        with patch(
            "app.graph.state.ActionAgent.run",
            new=AsyncMock(side_effect=Exception("Slack SDK crash")),
        ):
            result = await action_node(state)

        assert result["action_result"].briefing_sent is False
        assert "action" in result["run_errors"][0]


# ── TestPipelineModule ────────────────────────────────────────────────────────

class TestPipelineModule:

    def test_enrich_reviews_default_morning(self):
        """Morning briefing: enrich_reviews defaults to True."""
        from app.graph.pipeline import run_pipeline
        import inspect
        # Verify the default behaviour by checking make_initial_state defaults
        state = make_initial_state(briefing_type=BriefingType.DAILY_MORNING)
        assert state["enrich_reviews"] is True

    def test_enrich_reviews_default_eod(self):
        """EOD pulse: initial state enrich_reviews=True, node overrides to False."""
        state = make_initial_state(briefing_type=BriefingType.EOD_PULSE)
        # collector_node reads the briefing_type and sets enrich_reviews=False internally
        # The initial state still carries True; the node corrects it at runtime
        assert state["briefing_type"] == BriefingType.EOD_PULSE

    def test_pipeline_module_imports_without_error(self):
        """The pipeline module can be imported and the compiled graph is present."""
        from app.graph import pipeline
        assert hasattr(pipeline, "_pipeline")
        assert hasattr(pipeline, "run_pipeline")

    @pytest.mark.asyncio
    async def test_run_pipeline_end_to_end_mocked(self):
        """
        Full pipeline integration test — all four agents mocked.
        Verifies the graph wires all nodes together and returns final OpsState.
        """
        snapshot  = _make_snapshot()
        report    = _make_report(urgent_count=1)
        decision  = _make_decision(is_empty=False)
        ar        = _make_action_result(briefing_sent=True)

        with (
            patch("app.graph.state.CollectorAgent.run", new=AsyncMock(return_value=snapshot)),
            patch("app.graph.state.AnalystAgent.run",   new=AsyncMock(return_value=report)),
            patch("app.graph.state.DecisionAgent.run",  new=AsyncMock(return_value=decision)),
            patch("app.graph.state.ActionAgent.run",    new=AsyncMock(return_value=ar)),
        ):
            from app.graph.pipeline import run_pipeline
            final_state = await run_pipeline(briefing_type=BriefingType.DAILY_MORNING)

        # All four nodes ran and wrote their outputs into the state
        assert final_state["snapshot"]  is snapshot
        assert final_state["report"]    is report
        assert final_state["decision"]  is decision
        assert final_state["action_result"] is ar
        assert final_state["action_result"].briefing_sent is True
        assert final_state["run_errors"] == []
