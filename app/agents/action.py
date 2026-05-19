"""
app/agents/action.py
─────────────────────
Action Agent — "Do something about it."

Takes the DecisionOutput and executes the actions:
  1. Build the Slack Block Kit briefing payload
  2. Send via SlackClient
  3. (Optional) Create Linear tickets for auto-ticketable issues
  4. (Optional) Draft Notion page updates

Returns an ActionResult recording what was done and any failures.
"""

from __future__ import annotations

from typing import Optional

from app.core.config import settings
from app.core.constants import BriefingType
from app.core.logging import get_logger
from app.integrations import LinearClient, NotionClient, SlackClient
from app.integrations.models import SlackBriefingPayload
from app.integrations.slack import build_briefing_blocks
from app.agents.models import ActionResult, DecisionOutput

log = get_logger(__name__)


class ActionAgent:
    """
    Executes actions based on the Decision agent's output.

    Usage:
        agent = ActionAgent()
        result = await agent.run(decision, briefing_type=BriefingType.DAILY_MORNING)
    """

    def __init__(
        self,
        channel: Optional[str] = None,
        auto_create_tickets: bool = False,
        auto_update_notion: bool = False,
    ) -> None:
        """
        Args:
            channel:              Override the default Slack channel.
            auto_create_tickets:  If True, create Linear tickets for top urgent items.
            auto_update_notion:   If True, append update drafts to stale Notion pages.
        """
        self._channel = channel or settings.slack_channel_id
        self._auto_create_tickets = auto_create_tickets
        self._auto_update_notion = auto_update_notion

    async def run(
        self,
        decision: DecisionOutput,
        briefing_type: str = BriefingType.DAILY_MORNING,
    ) -> ActionResult:
        log.info(
            "action_start",
            briefing_type=briefing_type,
            urgent=len(decision.urgent_items),
            watch=len(decision.watch_items),
            channel=self._channel,
        )

        errors: list[str] = []
        tickets_created: list[str] = []
        notion_pages_updated: list[str] = []
        message_ts: Optional[str] = None

        # 1. Send the Slack briefing
        briefing_sent, message_ts = await self._send_briefing(decision, briefing_type, errors)

        # 2. Optionally create Linear tickets
        if self._auto_create_tickets and not decision.is_empty:
            tickets_created = await self._create_tickets(decision, errors)

        # 3. Optionally update Notion pages
        if self._auto_update_notion:
            notion_pages_updated = await self._update_notion_pages(decision, errors)

        result = ActionResult(
            briefing_sent=briefing_sent,
            channel=self._channel,
            message_ts=message_ts,
            tickets_created=tickets_created,
            notion_pages_updated=notion_pages_updated,
            errors=errors,
        )

        log.info(
            "action_done",
            briefing_sent=briefing_sent,
            tickets_created=len(tickets_created),
            notion_updated=len(notion_pages_updated),
            errors=errors,
        )
        return result

    # ── Private action methods ────────────────────────────────────────────────

    async def _send_briefing(
        self,
        decision: DecisionOutput,
        briefing_type: str,
        errors: list[str],
    ) -> tuple[bool, Optional[str]]:
        """Build Block Kit payload and send to Slack."""
        blocks = build_briefing_blocks(
            urgent_items=decision.urgent_items,
            watch_items=decision.watch_items,
            shipped_items=decision.shipped_items,
            suggestion=decision.suggestion,
        )

        fallback_text = self._build_fallback_text(decision, briefing_type)

        payload = SlackBriefingPayload(
            channel=self._channel,
            text=fallback_text,
            blocks=blocks,
            briefing_type=briefing_type,
        )

        async with SlackClient() as slack:
            ok = await slack.send_briefing(payload)

        if not ok:
            errors.append("Failed to send Slack briefing")

        return ok, None   # message_ts populated when Slack SDK returns it

    async def _create_tickets(
        self,
        decision: DecisionOutput,
        errors: list[str],
    ) -> list[str]:
        """
        Auto-create Linear tickets for the top urgent findings that
        don't already have a ticket (i.e., GitHub or Notion findings).
        """
        created_urls: list[str] = []
        from app.core.constants import DataSource

        # Only create tickets for non-Linear sources (GitHub PRs, Notion pages)
        targets = [
            f for f in decision.raw_findings
            if f.source != DataSource.LINEAR
        ][:2]   # max 2 auto-tickets per run

        async with LinearClient() as client:
            for finding in targets:
                try:
                    result = await client.create_ticket(
                        title=f"[OpsPilot] {finding.title}",
                        description=(
                            f"**Auto-created by OpsPilot**\n\n"
                            f"**Issue:** {finding.detail}\n"
                            f"**Source:** {finding.source} — {finding.item_url}\n"
                            f"**Days overdue:** {finding.days_overdue}"
                        ),
                    )
                    if result.get("url"):
                        created_urls.append(result["url"])
                        log.info("action_ticket_created", url=result["url"])
                except Exception as exc:
                    errors.append(f"Failed to create ticket for {finding.title}: {exc}")

        return created_urls

    async def _update_notion_pages(
        self,
        decision: DecisionOutput,
        errors: list[str],
    ) -> list[str]:
        """
        Append an update-needed note to stale Notion pages in the findings.
        The founder approves actual content changes manually.
        """
        from app.core.constants import DataSource

        updated: list[str] = []
        notion_findings = [
            f for f in decision.raw_findings
            if f.source == DataSource.NOTION
        ][:3]   # max 3 Notion updates per run

        if not notion_findings:
            return updated

        async with NotionClient() as client:
            for finding in notion_findings:
                note = (
                    f"⚠️ OpsPilot flagged this page as stale on "
                    f"{__import__('datetime').date.today()}. "
                    f"Please review and update or archive."
                )
                ok = await client.append_to_page(finding.item_id, note)
                if ok:
                    updated.append(finding.item_id)
                else:
                    errors.append(f"Failed to update Notion page {finding.item_id}")

        return updated

    # ── Formatting helpers ────────────────────────────────────────────────────

    @staticmethod
    def _build_fallback_text(decision: DecisionOutput, briefing_type: str) -> str:
        """
        Plain text fallback (shown in Slack notifications when blocks can't render).
        """
        label = {
            BriefingType.DAILY_MORNING: "Daily Briefing",
            BriefingType.EOD_PULSE: "End of Day Pulse",
            BriefingType.WEEKLY_DIGEST: "Weekly Digest",
            BriefingType.ON_DEMAND: "OpsPilot Response",
        }.get(briefing_type, "Briefing")

        if decision.is_empty:
            return f"🧭 OpsPilot {label} — Nothing urgent today. ✅"

        parts = [f"🧭 OpsPilot {label}"]
        if decision.urgent_items:
            parts.append(f"🔴 {len(decision.urgent_items)} urgent item(s)")
        if decision.watch_items:
            parts.append(f"🟡 {len(decision.watch_items)} to watch")
        if decision.suggestion:
            parts.append(f"💡 {decision.suggestion}")

        return " | ".join(parts)
