"""
app/agents/models.py
─────────────────────
Pydantic models shared across all four agents.

These define the intermediate data that flows between agents:
  CollectorAgent  →  CollectorSnapshot  (in app/integrations/models.py)
  AnalystAgent    →  AnalystReport
  DecisionAgent   →  DecisionOutput
  ActionAgent     →  ActionResult

AgentState is the LangGraph graph state (used in Step 4 / app/graph/).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from pydantic import BaseModel, Field, computed_field

from app.core.constants import ActionType, BriefingType, DataSource, Severity

if TYPE_CHECKING:
    from app.integrations.models import CollectorSnapshot


# ── Analyst output ────────────────────────────────────────────────────────────

class AnalystFinding(BaseModel):
    """
    A single issue identified by the Analyst agent.
    One finding = one item that may appear in the briefing.
    """

    source: DataSource                   # where this came from
    item_id: str                         # ticket ID, PR number, page ID
    item_url: str                        # direct link
    title: str                           # short human-readable label
    detail: str                          # one sentence of context
    severity: Severity                   # URGENT / WATCH / INFO
    days_overdue: int = 0                # how many days past threshold
    suggested_action: Optional[str] = None

    @computed_field  # type: ignore[misc]
    @property
    def finding_id(self) -> str:
        """
        Deterministic ID used for deduplication across runs.
        Same source + item_id always produces the same finding_id.
        """
        raw = f"{self.source}:{self.item_id}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    def to_slack_line(self) -> str:
        """Format this finding as a single Slack markdown line."""
        icon = {"urgent": "🔴", "watch": "🟡", "info": "✅"}.get(
            self.severity, "•"
        )
        line = f"{icon} *<{self.item_url}|{self.title}>*"
        if self.detail:
            line += f"\n   ↳ {self.detail}"
        if self.days_overdue:
            line += f" _(+{self.days_overdue}d overdue)_"
        return line


class AnalystReport(BaseModel):
    """Complete output of one Analyst agent run."""

    findings: list[AnalystFinding] = Field(default_factory=list)
    shipped_items: list[str] = Field(
        default_factory=list,
        description="Human-readable strings of things shipped (merged PRs, closed tickets)",
    )
    patterns: list[str] = Field(
        default_factory=list,
        description="LLM-generated cross-source pattern observations",
    )
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    llm_used: bool = False               # True if Gemini was called

    @computed_field  # type: ignore[misc]
    @property
    def urgent_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.URGENT)

    @computed_field  # type: ignore[misc]
    @property
    def watch_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.WATCH)


# ── Decision output ───────────────────────────────────────────────────────────

class DecisionOutput(BaseModel):
    """
    Final categorised content ready for the Action agent to send.
    Each list contains formatted Slack markdown strings.
    """

    urgent_items: list[str] = Field(default_factory=list)
    watch_items: list[str] = Field(default_factory=list)
    shipped_items: list[str] = Field(default_factory=list)
    suggestion: Optional[str] = None
    raw_findings: list[AnalystFinding] = Field(default_factory=list)

    @computed_field  # type: ignore[misc]
    @property
    def is_empty(self) -> bool:
        return not (self.urgent_items or self.watch_items or self.shipped_items)


# ── Action output ─────────────────────────────────────────────────────────────

class ActionResult(BaseModel):
    """Records what the Action agent actually did."""

    briefing_sent: bool = False
    channel: str = ""
    message_ts: Optional[str] = None     # Slack message timestamp (for threading)
    tickets_created: list[str] = Field(
        default_factory=list,
        description="URLs of Linear tickets created",
    )
    notion_pages_updated: list[str] = Field(
        default_factory=list,
        description="Page IDs of Notion pages updated",
    )
    errors: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[misc]
    @property
    def success(self) -> bool:
        return self.briefing_sent and len(self.errors) == 0


# ── LangGraph state (used in app/graph/) ─────────────────────────────────────

class AgentState(BaseModel):
    """
    The shared state object that flows through the LangGraph pipeline.
    Each agent reads from and writes to this object.
    Defined here so agents and graph both import from one place.
    """

    briefing_type: str = BriefingType.DAILY_MORNING
    snapshot: Optional[object] = None    # CollectorSnapshot (avoid circular import)
    report: Optional[AnalystReport] = None
    decision: Optional[DecisionOutput] = None
    action_result: Optional[ActionResult] = None
    run_errors: list[str] = Field(default_factory=list)
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
