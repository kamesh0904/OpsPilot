"""
app/agents/analyst.py
──────────────────────
Analyst Agent — "What does this data mean?"

Two-stage analysis:
  Stage 1 — Rule-based (always runs, no LLM needed):
    Flags stale tickets, stale PRs, unassigned tickets, PRs with no
    reviewer, stale Notion docs. Uses thresholds from settings.

  Stage 2 — LLM-based (Gemini, runs if API key is set):
    Sends all findings + raw data to Gemini in one prompt.
    Asks it to identify cross-source patterns, add context, and
    generate a single actionable suggestion for the founder.

The two-stage design means the agent degrades gracefully:
  - Gemini down or no API key → rule-based findings still sent
  - Rule-based finds nothing → Gemini still looks for subtle patterns
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage

from app.core.config import settings
from app.core.constants import DataSource, Severity
from app.core.logging import get_logger
from app.integrations.models import CollectorSnapshot, LinearTicket, PullRequest, NotionPage
from app.agents.models import AnalystFinding, AnalystReport

log = get_logger(__name__)


class AnalystAgent:
    """
    Analyses a CollectorSnapshot and returns an AnalystReport.

    Usage:
        agent = AnalystAgent()
        report = await agent.run(snapshot)
    """

    def __init__(self, use_llm: bool = True) -> None:
        """
        Args:
            use_llm: Set to False to skip Gemini and use rule-based only.
                     Useful for testing or when API key is not set.
        """
        self._use_llm = use_llm and bool(settings.google_api_key)

    async def run(self, snapshot: CollectorSnapshot) -> AnalystReport:
        log.info(
            "analyst_start",
            tickets=len(snapshot.linear_tickets),
            prs=len(snapshot.pull_requests),
            pages=len(snapshot.notion_pages),
        )

        # Stage 1: rule-based findings
        findings = self._rule_based_analysis(snapshot)

        # Stage 2: LLM enrichment
        patterns: list[str] = []
        suggestion: Optional[str] = None
        llm_used = False

        if self._use_llm:
            try:
                patterns, suggestion = await self._llm_analysis(snapshot, findings)
                llm_used = True
            except Exception as exc:
                log.warning("analyst_llm_failed", error=str(exc))
                # Gracefully continue with rule-based results only

        # Build shipped items from recently merged PRs and closed tickets
        shipped = self._build_shipped_summary(snapshot)

        # Attach LLM suggestion to the top urgent finding if present
        if suggestion and findings:
            urgent = [f for f in findings if f.severity == Severity.URGENT]
            if urgent:
                urgent[0] = urgent[0].model_copy(
                    update={"suggested_action": suggestion}
                )
                findings = [
                    urgent[0] if f.finding_id == urgent[0].finding_id else f
                    for f in findings
                ]

        report = AnalystReport(
            findings=findings,
            shipped_items=shipped,
            patterns=patterns,
            llm_used=llm_used,
        )

        log.info(
            "analyst_done",
            urgent=report.urgent_count,
            watch=report.watch_count,
            shipped=len(report.shipped_items),
            llm_used=llm_used,
        )
        return report

    # ── Stage 1: Rule-based ───────────────────────────────────────────────────

    def _rule_based_analysis(self, snapshot: CollectorSnapshot) -> list[AnalystFinding]:
        findings: list[AnalystFinding] = []
        findings.extend(self._analyse_tickets(snapshot.linear_tickets))
        findings.extend(self._analyse_prs(snapshot.pull_requests))
        findings.extend(self._analyse_notion(snapshot.notion_pages))
        # Sort: urgent first, then by days_overdue descending
        findings.sort(
            key=lambda f: (0 if f.severity == Severity.URGENT else 1, -f.days_overdue)
        )
        return findings

    def _analyse_tickets(self, tickets: list[LinearTicket]) -> list[AnalystFinding]:
        findings = []
        threshold = settings.stale_ticket_days

        for ticket in tickets:
            if not ticket.is_active:
                continue

            overdue = ticket.days_since_update - threshold

            if ticket.is_unassigned and ticket.days_since_update >= threshold:
                findings.append(AnalystFinding(
                    source=DataSource.LINEAR,
                    item_id=ticket.id,
                    item_url=ticket.url,
                    title=ticket.title,
                    detail=f"Unassigned and no update for {ticket.days_since_update} days",
                    severity=Severity.URGENT,
                    days_overdue=max(0, overdue),
                    suggested_action="Assign this ticket before standup",
                ))
            elif ticket.days_since_update >= threshold:
                findings.append(AnalystFinding(
                    source=DataSource.LINEAR,
                    item_id=ticket.id,
                    item_url=ticket.url,
                    title=ticket.title,
                    detail=(
                        f"Assigned to {ticket.assignee}, "
                        f"no update for {ticket.days_since_update} days"
                    ),
                    severity=Severity.URGENT if overdue > 3 else Severity.WATCH,
                    days_overdue=max(0, overdue),
                ))

        return findings

    def _analyse_prs(self, prs: list[PullRequest]) -> list[AnalystFinding]:
        findings = []
        threshold = settings.stale_pr_days

        for pr in prs:
            if pr.state != "open" or pr.draft:
                continue

            overdue = pr.days_open - threshold

            if pr.has_no_reviewer and pr.days_open >= threshold:
                findings.append(AnalystFinding(
                    source=DataSource.GITHUB,
                    item_id=str(pr.number),
                    item_url=pr.url,
                    title=f"PR #{pr.number}: {pr.title}",
                    detail=f"Open {pr.days_open} days with no reviewer assigned ({pr.repo})",
                    severity=Severity.URGENT,
                    days_overdue=max(0, overdue),
                    suggested_action=f"Assign a reviewer to PR #{pr.number}",
                ))
            elif pr.days_open >= threshold:
                findings.append(AnalystFinding(
                    source=DataSource.GITHUB,
                    item_id=str(pr.number),
                    item_url=pr.url,
                    title=f"PR #{pr.number}: {pr.title}",
                    detail=(
                        f"Open {pr.days_open} days in {pr.repo}, "
                        f"review: {pr.review_decision or 'pending'}"
                    ),
                    severity=Severity.URGENT if overdue > 2 else Severity.WATCH,
                    days_overdue=max(0, overdue),
                ))

        return findings

    def _analyse_notion(self, pages: list[NotionPage]) -> list[AnalystFinding]:
        findings = []
        threshold = settings.stale_notion_doc_days

        for page in pages:
            if page.archived:
                continue

            overdue = page.days_since_edit - threshold
            if page.days_since_edit >= threshold:
                findings.append(AnalystFinding(
                    source=DataSource.NOTION,
                    item_id=page.id,
                    item_url=page.url,
                    title=page.title,
                    detail=f"Last edited {page.days_since_edit} days ago",
                    severity=Severity.WATCH,
                    days_overdue=max(0, overdue),
                    suggested_action="Review and update or archive this page",
                ))

        return findings

    def _build_shipped_summary(self, snapshot: CollectorSnapshot) -> list[str]:
        """Build the 'shipped this week' summary lines."""
        items = []

        # Count merged PRs (state = "closed" would be in a different fetch;
        # for now count non-stale PRs that were recently updated)
        recent_prs = [
            pr for pr in snapshot.pull_requests
            if pr.state == "open" and pr.days_since_update == 0
        ]
        if recent_prs:
            items.append(f"{len(recent_prs)} PR(s) updated today")

        closed_tickets = [
            t for t in snapshot.linear_tickets
            if not t.is_active and t.days_since_update <= 7
        ]
        if closed_tickets:
            items.append(f"{len(closed_tickets)} ticket(s) closed this week")

        return items

    # ── Stage 2: LLM analysis ─────────────────────────────────────────────────

    async def _llm_analysis(
        self,
        snapshot: CollectorSnapshot,
        rule_findings: list[AnalystFinding],
    ) -> tuple[list[str], Optional[str]]:
        """
        Ask Gemini to identify patterns and generate one actionable suggestion.

        Returns:
            (patterns, suggestion) — both may be empty/None on failure.
        """
        prompt = self._build_prompt(snapshot, rule_findings)

        llm = ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            google_api_key=settings.google_api_key,
            temperature=0.3,
        )

        response = await llm.ainvoke([HumanMessage(content=prompt)])
        raw = response.content if hasattr(response, "content") else str(response)

        return self._parse_llm_response(raw)

    def _build_prompt(
        self,
        snapshot: CollectorSnapshot,
        findings: list[AnalystFinding],
    ) -> str:
        """Build the analysis prompt sent to Gemini."""
        tickets_summary = "\n".join(
            f"- [{t.status}] {t.title} | assignee: {t.assignee or 'none'} "
            f"| updated {t.days_since_update}d ago"
            for t in snapshot.linear_tickets[:50]   # cap to avoid token overflow
        )
        prs_summary = "\n".join(
            f"- PR #{p.number} {p.title} | {p.repo} | "
            f"open {p.days_open}d | reviewers: {p.requested_reviewers or 'none'}"
            for p in snapshot.pull_requests[:30]
        )
        findings_summary = "\n".join(
            f"- [{f.severity.upper()}] {f.title}: {f.detail}"
            for f in findings
        )

        return f"""You are an ops analyst for a startup. Analyze the following data and respond in JSON.

## Linear Tickets (active)
{tickets_summary or "None"}

## GitHub Pull Requests (open)
{prs_summary or "None"}

## Rule-based findings already identified
{findings_summary or "None"}

## Your task
Identify patterns the rule-based system missed. Look for:
- Recurring blockers (same person/area keeps appearing)
- Cross-source issues (a PR and ticket are clearly related and both stalled)
- Positive signals worth highlighting

Respond ONLY with valid JSON in this exact format:
{{
  "patterns": ["pattern 1 sentence", "pattern 2 sentence"],
  "suggestion": "One specific thing the founder should do today"
}}

Keep each pattern under 20 words. Suggestion must be one actionable sentence."""

    @staticmethod
    def _parse_llm_response(raw: str) -> tuple[list[str], Optional[str]]:
        """Parse Gemini's JSON response. Returns empty defaults on parse failure."""
        try:
            # Strip markdown code fences if present
            clean = raw.strip()
            if clean.startswith("```"):
                clean = "\n".join(clean.split("\n")[1:-1])
            data = json.loads(clean)
            patterns = data.get("patterns", [])
            suggestion = data.get("suggestion")
            return patterns, suggestion
        except Exception:
            log.warning("analyst_llm_parse_failed", raw_preview=raw[:200])
            return [], None
