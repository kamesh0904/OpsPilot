"""
app/agents/decision.py
───────────────────────
Decision Agent — "What actually matters right now?"

Takes the AnalystReport and applies priority logic to produce a clean
DecisionOutput that tells the Action agent exactly what to include in
the briefing — and what to silently skip.

Key responsibilities:
  - Separate findings into URGENT / WATCH / INFO buckets
  - Deduplicate: skip findings that were already flagged in a recent run
    (currently uses an in-memory set; DB persistence added in Step 5)
  - Cap the number of items per bucket (prevent info overload)
  - Pick the single best suggestion from all findings
"""

from __future__ import annotations

from app.core.config import settings
from app.core.constants import BriefingType, Severity
from app.core.logging import get_logger
from app.agents.models import AnalystFinding, AnalystReport, DecisionOutput

log = get_logger(__name__)

# How many items max to show per section in the briefing
_MAX_URGENT = 5
_MAX_WATCH = 4

# In-memory dedup store: finding_ids seen in the last run per briefing_type.
# Replaced with DB-backed persistence in Step 5 (app/db/).
_seen_finding_ids: dict[str, set[str]] = {}


class DecisionAgent:
    """
    Filters and prioritises AnalystReport findings into a briefing-ready
    DecisionOutput.

    Usage:
        agent = DecisionAgent()
        decision = await agent.run(report, briefing_type=BriefingType.DAILY_MORNING)
    """

    async def run(
        self,
        report: AnalystReport,
        briefing_type: str = BriefingType.DAILY_MORNING,
    ) -> DecisionOutput:
        log.info(
            "decision_start",
            briefing_type=briefing_type,
            total_findings=len(report.findings),
        )

        # Retrieve seen IDs for this briefing type (dedup across runs)
        seen = _seen_finding_ids.get(briefing_type, set())

        # Filter, deduplicate, then bucket
        urgent, watch = self._bucket_findings(report.findings, seen)

        # Cap per bucket
        urgent = urgent[:_MAX_URGENT]
        watch = watch[:_MAX_WATCH]

        # Pick the best suggestion
        suggestion = self._pick_suggestion(urgent + watch, report)

        # Format each finding as a Slack markdown line
        urgent_lines = [f.to_slack_line() for f in urgent]
        watch_lines = [f.to_slack_line() for f in watch]

        # Build shipped summary
        shipped = self._build_shipped_lines(report)

        # Update the seen set for next run
        new_seen = {f.finding_id for f in urgent + watch}
        _seen_finding_ids[briefing_type] = new_seen

        output = DecisionOutput(
            urgent_items=urgent_lines,
            watch_items=watch_lines,
            shipped_items=shipped,
            suggestion=suggestion,
            raw_findings=urgent + watch,
        )

        log.info(
            "decision_done",
            urgent=len(urgent_lines),
            watch=len(watch_lines),
            shipped=len(shipped),
            is_empty=output.is_empty,
        )
        return output

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _bucket_findings(
        self,
        findings: list[AnalystFinding],
        seen: set[str],
    ) -> tuple[list[AnalystFinding], list[AnalystFinding]]:
        """
        Split findings into urgent/watch, skipping ones already seen.

        A finding is skipped if its finding_id was in the previous run's
        output AND it hasn't become more overdue since then.
        Currently: skip if seen (simple); Step 5 adds time-based re-surfacing.
        """
        urgent, watch = [], []

        for finding in findings:
            if finding.finding_id in seen:
                log.debug(
                    "decision_skipped_duplicate",
                    finding_id=finding.finding_id,
                    title=finding.title,
                )
                continue

            if finding.severity == Severity.URGENT:
                urgent.append(finding)
            elif finding.severity == Severity.WATCH:
                watch.append(finding)
            # INFO severity findings are counted in shipped, not flagged

        return urgent, watch

    def _pick_suggestion(
        self,
        findings: list[AnalystFinding],
        report: AnalystReport,
    ) -> str | None:
        """
        Return the single most actionable suggestion:
          1. Use suggested_action from the most urgent finding (if set by Analyst)
          2. Else auto-generate a generic one from the top finding
          3. Else None (no suggestion shown in briefing)
        """
        for finding in findings:
            if finding.suggested_action:
                return finding.suggested_action

        if findings:
            top = findings[0]
            return f"Address {top.title[:60]} — it's been flagged for {top.days_overdue} days."

        return None

    def _build_shipped_lines(self, report: AnalystReport) -> list[str]:
        """Convert AnalystReport.shipped_items into formatted lines."""
        lines = list(report.shipped_items)

        # Add LLM-detected patterns as INFO lines
        for pattern in report.patterns[:2]:     # max 2 patterns in shipped
            lines.append(f"💡 {pattern}")

        return lines
