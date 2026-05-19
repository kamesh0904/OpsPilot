"""
app/integrations/models.py
───────────────────────────
Shared Pydantic data models that every integration returns.

The Collector Agent calls each integration client and receives these
structured objects. Every downstream agent (Analyst, Decision, Action)
works exclusively with these models — never with raw API responses.

This keeps the agent code decoupled from the specific shape of each
external API. If Linear changes their GraphQL schema, only linear.py
changes; the agents are untouched.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, computed_field


def _days_ago(dt: datetime) -> int:
    """Return how many full days ago a UTC datetime was."""
    now = datetime.now(timezone.utc)
    # Make sure dt is timezone-aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    return max(0, delta.days)


# ── Linear ───────────────────────────────────────────────────────────────────

class LinearTicket(BaseModel):
    """A single Linear issue/ticket."""

    id: str
    title: str
    status: str                         # e.g. "In Progress", "Todo", "Done"
    status_type: str                    # e.g. "started", "unstarted", "completed"
    assignee: Optional[str] = None      # display name or None if unassigned
    team_id: str
    updated_at: datetime
    created_at: datetime
    url: str
    comment_count: int = 0
    priority: int = 0                   # 0=No priority, 1=Urgent, 2=High, 3=Med, 4=Low
    labels: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[misc]
    @property
    def days_since_update(self) -> int:
        return _days_ago(self.updated_at)

    @computed_field  # type: ignore[misc]
    @property
    def is_unassigned(self) -> bool:
        return self.assignee is None

    @computed_field  # type: ignore[misc]
    @property
    def is_active(self) -> bool:
        """True if the ticket is in a non-terminal state."""
        return self.status_type not in ("completed", "cancelled")


# ── GitHub ───────────────────────────────────────────────────────────────────

class PullRequest(BaseModel):
    """A single GitHub pull request."""

    number: int
    title: str
    repo: str                                   # "org/repo-name"
    state: str                                   # "open" | "closed"
    author: str
    created_at: datetime
    updated_at: datetime
    url: str
    draft: bool = False
    requested_reviewers: list[str] = Field(default_factory=list)
    review_decision: Optional[str] = None        # "APPROVED" | "CHANGES_REQUESTED" | None
    base_branch: str = "main"
    head_branch: str = ""
    additions: int = 0
    deletions: int = 0

    @computed_field  # type: ignore[misc]
    @property
    def days_open(self) -> int:
        return _days_ago(self.created_at)

    @computed_field  # type: ignore[misc]
    @property
    def days_since_update(self) -> int:
        return _days_ago(self.updated_at)

    @computed_field  # type: ignore[misc]
    @property
    def has_no_reviewer(self) -> bool:
        return len(self.requested_reviewers) == 0 and self.review_decision is None


# ── Notion ───────────────────────────────────────────────────────────────────

class NotionPage(BaseModel):
    """A single Notion page."""

    id: str
    title: str
    url: str
    last_edited: datetime
    created_time: datetime
    last_edited_by: Optional[str] = None         # user display name
    parent_type: str = "workspace"               # "workspace" | "database" | "page"
    archived: bool = False

    @computed_field  # type: ignore[misc]
    @property
    def days_since_edit(self) -> int:
        return _days_ago(self.last_edited)

    @computed_field  # type: ignore[misc]
    @property
    def is_stale(self) -> bool:
        """Convenience — actual threshold comes from settings in the Decision agent."""
        return self.days_since_edit > 30


# ── Slack ────────────────────────────────────────────────────────────────────

class SlackMessage(BaseModel):
    """An inbound Slack message directed at OpsPilot."""

    channel: str
    text: str
    user_id: str
    username: Optional[str] = None
    timestamp: str                               # Slack ts format "1234567890.123456"
    thread_ts: Optional[str] = None              # set if this is a thread reply
    is_mention: bool = False                     # True if @opspilot was mentioned


class SlackBriefingPayload(BaseModel):
    """The fully assembled briefing message ready to post to Slack."""

    channel: str
    text: str                                    # fallback plain text
    blocks: list[dict]                           # Slack Block Kit blocks
    briefing_type: str                           # BriefingType enum value


# ── Collector snapshot (what the Collector Agent produces) ───────────────────

class CollectorSnapshot(BaseModel):
    """
    Complete snapshot of data fetched from all integrations in a single run.
    This is passed from the Collector Agent to the Analyst Agent.
    """

    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    linear_tickets: list[LinearTicket] = Field(default_factory=list)
    pull_requests: list[PullRequest] = Field(default_factory=list)
    notion_pages: list[NotionPage] = Field(default_factory=list)
    errors: dict[str, str] = Field(
        default_factory=dict,
        description="source → error message for any integration that failed",
    )

    @computed_field  # type: ignore[misc]
    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0
