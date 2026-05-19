"""
tests/test_agents.py
──────────────────────
Unit tests for app/agents/ — no real API calls, no Gemini, no Slack.

Run with: python -m pytest tests/test_agents.py -v
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.models import (
    ActionResult,
    AgentState,
    AnalystFinding,
    AnalystReport,
    DecisionOutput,
)
from app.agents.analyst import AnalystAgent
from app.agents.decision import DecisionAgent, _seen_finding_ids
from app.agents.action import ActionAgent
from app.core.constants import BriefingType, DataSource, Severity
from app.integrations.models import (
    CollectorSnapshot,
    LinearTicket,
    NotionPage,
    PullRequest,
)


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

def _ticket(days_old=6, assignee=None, status_type="started", **kw) -> LinearTicket:
    return LinearTicket(
        id=kw.get("id", "T-001"),
        title=kw.get("title", "Fix login bug"),
        status="In Progress",
        status_type=status_type,
        assignee=assignee,
        team_id="team-1",
        updated_at=datetime.now(timezone.utc) - timedelta(days=days_old),
        created_at=datetime.now(timezone.utc) - timedelta(days=days_old + 2),
        url="https://linear.app/t/T-001",
    )


def _pr(days_old=5, reviewers=None, review_decision=None, **kw) -> PullRequest:
    return PullRequest(
        number=kw.get("number", 47),
        title=kw.get("title", "Auth refactor"),
        repo="org/api",
        state="open",
        author="alice",
        created_at=datetime.now(timezone.utc) - timedelta(days=days_old),
        updated_at=datetime.now(timezone.utc) - timedelta(days=1),
        url="https://github.com/org/api/pull/47",
        requested_reviewers=reviewers or [],
        review_decision=review_decision,
    )


def _notion(days_old=50, **kw) -> NotionPage:
    return NotionPage(
        id=kw.get("id", "page-1"),
        title=kw.get("title", "Q4 Roadmap"),
        url="https://notion.so/q4",
        last_edited=datetime.now(timezone.utc) - timedelta(days=days_old),
        created_time=datetime.now(timezone.utc) - timedelta(days=days_old + 10),
    )


def _snapshot(**kw) -> CollectorSnapshot:
    return CollectorSnapshot(
        linear_tickets=kw.get("linear_tickets", []),
        pull_requests=kw.get("pull_requests", []),
        notion_pages=kw.get("notion_pages", []),
        errors=kw.get("errors", {}),
    )


def _finding(severity=Severity.URGENT, source=DataSource.LINEAR, **kw) -> AnalystFinding:
    return AnalystFinding(
        source=source,
        item_id=kw.get("item_id", "T-001"),
        item_url="https://example.com",
        title=kw.get("title", "Stale ticket"),
        detail="No update in 6 days",
        severity=severity,
        days_overdue=kw.get("days_overdue", 1),
        suggested_action=kw.get("suggested_action"),
    )


# ══════════════════════════════════════════════════════════════════════════════
# AnalystFinding model
# ══════════════════════════════════════════════════════════════════════════════

class TestAnalystFinding:
    def test_finding_id_is_deterministic(self):
        f1 = _finding(item_id="T-001", source=DataSource.LINEAR)
        f2 = _finding(item_id="T-001", source=DataSource.LINEAR)
        assert f1.finding_id == f2.finding_id

    def test_finding_id_differs_by_source(self):
        f1 = _finding(item_id="001", source=DataSource.LINEAR)
        f2 = _finding(item_id="001", source=DataSource.GITHUB)
        assert f1.finding_id != f2.finding_id

    def test_to_slack_line_urgent(self):
        f = _finding(severity=Severity.URGENT, title="PR #47 stale", days_overdue=2)
        line = f.to_slack_line()
        assert "🔴" in line
        assert "PR #47 stale" in line
        assert "+2d overdue" in line

    def test_to_slack_line_watch(self):
        f = _finding(severity=Severity.WATCH, title="Old page", days_overdue=0)
        line = f.to_slack_line()
        assert "🟡" in line
        assert "overdue" not in line   # 0 days_overdue → no overdue label

    def test_to_slack_line_includes_detail(self):
        f = AnalystFinding(
            source=DataSource.LINEAR, item_id="x", item_url="http://a.com",
            title="T", detail="Some detail here", severity=Severity.WATCH,
        )
        assert "Some detail here" in f.to_slack_line()


# ══════════════════════════════════════════════════════════════════════════════
# AnalystReport model
# ══════════════════════════════════════════════════════════════════════════════

class TestAnalystReport:
    def test_urgent_count(self):
        report = AnalystReport(findings=[
            _finding(severity=Severity.URGENT),
            _finding(severity=Severity.URGENT, item_id="T-002"),
            _finding(severity=Severity.WATCH, item_id="T-003"),
        ])
        assert report.urgent_count == 2
        assert report.watch_count == 1

    def test_empty_report(self):
        report = AnalystReport()
        assert report.urgent_count == 0
        assert report.shipped_items == []


# ══════════════════════════════════════════════════════════════════════════════
# DecisionOutput model
# ══════════════════════════════════════════════════════════════════════════════

class TestDecisionOutput:
    def test_is_empty_true(self):
        d = DecisionOutput()
        assert d.is_empty is True

    def test_is_empty_false_urgent(self):
        d = DecisionOutput(urgent_items=["one item"])
        assert d.is_empty is False

    def test_is_empty_false_shipped(self):
        d = DecisionOutput(shipped_items=["4 PRs merged"])
        assert d.is_empty is False


# ══════════════════════════════════════════════════════════════════════════════
# AnalystAgent — rule-based (no LLM)
# ══════════════════════════════════════════════════════════════════════════════

class TestAnalystAgentRuleBased:
    """All tests use use_llm=False to avoid needing a Gemini key."""

    @pytest.mark.asyncio
    async def test_stale_unassigned_ticket_is_urgent(self):
        snap = _snapshot(linear_tickets=[_ticket(days_old=6, assignee=None)])
        agent = AnalystAgent(use_llm=False)
        report = await agent.run(snap)
        assert any(f.severity == Severity.URGENT for f in report.findings)
        assert any(DataSource.LINEAR == f.source for f in report.findings)

    @pytest.mark.asyncio
    async def test_fresh_ticket_not_flagged(self):
        snap = _snapshot(linear_tickets=[_ticket(days_old=2, assignee="Alice")])
        agent = AnalystAgent(use_llm=False)
        report = await agent.run(snap)
        linear_findings = [f for f in report.findings if f.source == DataSource.LINEAR]
        assert len(linear_findings) == 0

    @pytest.mark.asyncio
    async def test_completed_ticket_not_flagged(self):
        snap = _snapshot(linear_tickets=[_ticket(days_old=10, status_type="completed")])
        agent = AnalystAgent(use_llm=False)
        report = await agent.run(snap)
        assert len(report.findings) == 0

    @pytest.mark.asyncio
    async def test_stale_pr_no_reviewer_is_urgent(self):
        snap = _snapshot(pull_requests=[_pr(days_old=5, reviewers=[])])
        agent = AnalystAgent(use_llm=False)
        report = await agent.run(snap)
        github_findings = [f for f in report.findings if f.source == DataSource.GITHUB]
        assert len(github_findings) == 1
        assert github_findings[0].severity == Severity.URGENT

    @pytest.mark.asyncio
    async def test_fresh_pr_not_flagged(self):
        snap = _snapshot(pull_requests=[_pr(days_old=1)])
        agent = AnalystAgent(use_llm=False)
        report = await agent.run(snap)
        github_findings = [f for f in report.findings if f.source == DataSource.GITHUB]
        assert len(github_findings) == 0

    @pytest.mark.asyncio
    async def test_draft_pr_not_flagged(self):
        pr = _pr(days_old=10)
        pr = pr.model_copy(update={"draft": True})
        snap = _snapshot(pull_requests=[pr])
        agent = AnalystAgent(use_llm=False)
        report = await agent.run(snap)
        assert len(report.findings) == 0

    @pytest.mark.asyncio
    async def test_stale_notion_page_flagged(self):
        snap = _snapshot(notion_pages=[_notion(days_old=35)])
        agent = AnalystAgent(use_llm=False)
        report = await agent.run(snap)
        notion_findings = [f for f in report.findings if f.source == DataSource.NOTION]
        assert len(notion_findings) == 1
        assert notion_findings[0].severity == Severity.WATCH

    @pytest.mark.asyncio
    async def test_archived_notion_page_not_flagged(self):
        page = _notion(days_old=100)
        page = page.model_copy(update={"archived": True})
        snap = _snapshot(notion_pages=[page])
        agent = AnalystAgent(use_llm=False)
        report = await agent.run(snap)
        assert len(report.findings) == 0

    @pytest.mark.asyncio
    async def test_findings_sorted_urgent_first(self):
        snap = _snapshot(
            linear_tickets=[_ticket(days_old=6, assignee=None)],   # urgent
            notion_pages=[_notion(days_old=35)],                    # watch
        )
        agent = AnalystAgent(use_llm=False)
        report = await agent.run(snap)
        assert report.findings[0].severity == Severity.URGENT

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_gracefully(self):
        """If LLM raises, rule-based findings still returned."""
        snap = _snapshot(linear_tickets=[_ticket(days_old=6)])
        agent = AnalystAgent(use_llm=True)

        with patch.object(agent, "_llm_analysis", side_effect=Exception("API down")):
            report = await agent.run(snap)

        assert len(report.findings) > 0
        assert report.llm_used is False

    def test_parse_llm_response_valid_json(self):
        raw = '{"patterns": ["Backend stalls on Fridays"], "suggestion": "Check in with backend team"}'
        patterns, suggestion = AnalystAgent._parse_llm_response(raw)
        assert patterns == ["Backend stalls on Fridays"]
        assert suggestion == "Check in with backend team"

    def test_parse_llm_response_invalid_json(self):
        patterns, suggestion = AnalystAgent._parse_llm_response("not json at all")
        assert patterns == []
        assert suggestion is None

    def test_parse_llm_response_strips_code_fences(self):
        raw = "```json\n{\"patterns\": [], \"suggestion\": \"Do X\"}\n```"
        _, suggestion = AnalystAgent._parse_llm_response(raw)
        assert suggestion == "Do X"


# ══════════════════════════════════════════════════════════════════════════════
# DecisionAgent
# ══════════════════════════════════════════════════════════════════════════════

class TestDecisionAgent:
    def setup_method(self):
        """Clear the in-memory dedup store before each test."""
        _seen_finding_ids.clear()

    @pytest.mark.asyncio
    async def test_urgent_findings_go_to_urgent_bucket(self):
        report = AnalystReport(findings=[_finding(severity=Severity.URGENT)])
        agent = DecisionAgent()
        decision = await agent.run(report)
        assert len(decision.urgent_items) == 1
        assert len(decision.watch_items) == 0

    @pytest.mark.asyncio
    async def test_watch_findings_go_to_watch_bucket(self):
        report = AnalystReport(findings=[_finding(severity=Severity.WATCH)])
        agent = DecisionAgent()
        decision = await agent.run(report)
        assert len(decision.watch_items) == 1
        assert len(decision.urgent_items) == 0

    @pytest.mark.asyncio
    async def test_dedup_skips_seen_findings(self):
        finding = _finding(severity=Severity.URGENT)
        report = AnalystReport(findings=[finding])
        agent = DecisionAgent()

        # First run — finding appears
        d1 = await agent.run(report, briefing_type=BriefingType.DAILY_MORNING)
        assert len(d1.urgent_items) == 1

        # Second run — same finding should be deduplicated
        d2 = await agent.run(report, briefing_type=BriefingType.DAILY_MORNING)
        assert len(d2.urgent_items) == 0

    @pytest.mark.asyncio
    async def test_dedup_is_per_briefing_type(self):
        """Findings seen in morning brief still appear in EOD pulse."""
        finding = _finding(severity=Severity.URGENT)
        report = AnalystReport(findings=[finding])
        agent = DecisionAgent()

        await agent.run(report, briefing_type=BriefingType.DAILY_MORNING)
        d_eod = await agent.run(report, briefing_type=BriefingType.EOD_PULSE)
        assert len(d_eod.urgent_items) == 1   # separate dedup store

    @pytest.mark.asyncio
    async def test_caps_urgent_at_max(self):
        findings = [
            _finding(severity=Severity.URGENT, item_id=f"T-{i}", title=f"Issue {i}")
            for i in range(10)   # 10 findings, cap is 5
        ]
        report = AnalystReport(findings=findings)
        agent = DecisionAgent()
        decision = await agent.run(report)
        assert len(decision.urgent_items) <= 5

    @pytest.mark.asyncio
    async def test_suggestion_from_finding_action(self):
        finding = _finding(
            severity=Severity.URGENT,
            suggested_action="Assign PR #47 before standup",
        )
        report = AnalystReport(findings=[finding])
        agent = DecisionAgent()
        decision = await agent.run(report)
        assert decision.suggestion == "Assign PR #47 before standup"

    @pytest.mark.asyncio
    async def test_empty_report_produces_empty_decision(self):
        agent = DecisionAgent()
        decision = await agent.run(AnalystReport())
        assert decision.is_empty is True
        assert decision.suggestion is None


# ══════════════════════════════════════════════════════════════════════════════
# ActionAgent — _build_fallback_text
# ══════════════════════════════════════════════════════════════════════════════

class TestActionAgentFallbackText:
    def test_empty_decision_fallback(self):
        text = ActionAgent._build_fallback_text(DecisionOutput(), BriefingType.DAILY_MORNING)
        assert "Nothing urgent" in text

    def test_urgent_items_in_fallback(self):
        d = DecisionOutput(urgent_items=["item1", "item2"])
        text = ActionAgent._build_fallback_text(d, BriefingType.DAILY_MORNING)
        assert "2 urgent" in text

    def test_suggestion_in_fallback(self):
        d = DecisionOutput(urgent_items=["x"], suggestion="Do the thing")
        text = ActionAgent._build_fallback_text(d, BriefingType.EOD_PULSE)
        assert "Do the thing" in text
        assert "End of Day Pulse" in text

    def test_weekly_digest_label(self):
        text = ActionAgent._build_fallback_text(DecisionOutput(), BriefingType.WEEKLY_DIGEST)
        assert "Weekly Digest" in text


# ══════════════════════════════════════════════════════════════════════════════
# AgentState model
# ══════════════════════════════════════════════════════════════════════════════

class TestAgentState:
    def test_default_briefing_type(self):
        state = AgentState()
        assert state.briefing_type == BriefingType.DAILY_MORNING

    def test_run_errors_starts_empty(self):
        state = AgentState()
        assert state.run_errors == []

    def test_state_with_report(self):
        report = AnalystReport(findings=[_finding()])
        state = AgentState(report=report)
        assert state.report is not None
        assert len(state.report.findings) == 1
