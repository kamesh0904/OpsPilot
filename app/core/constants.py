"""
app/core/constants.py
──────────────────────
Domain constants and enums shared across the entire app.
Nothing here should depend on settings or other app modules.
"""

import sys
from enum import Enum

# StrEnum was introduced in Python 3.11; provide a compatible shim for 3.10.
if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    class StrEnum(str, Enum):  # type: ignore[no-redef]
        """String enum compatible with Python 3.10."""


# ── Agent identifiers ────────────────────────────────────────────────────────

class AgentName(StrEnum):
    COLLECTOR = "collector"
    ANALYST   = "analyst"
    DECISION  = "decision"
    ACTION    = "action"


# ── Integration source names ─────────────────────────────────────────────────

class DataSource(StrEnum):
    LINEAR = "linear"
    NOTION = "notion"
    GITHUB = "github"
    SLACK  = "slack"


# ── Alert severity levels (used in briefing output) ──────────────────────────

class Severity(StrEnum):
    URGENT = "urgent"    # 🔴 — needs founder action today
    WATCH  = "watch"     # 🟡 — monitor, may escalate
    INFO   = "info"      # ✅ — positive signal / shipped


# ── Briefing types ───────────────────────────────────────────────────────────

class BriefingType(StrEnum):
    DAILY_MORNING = "daily_morning"   # 9 AM
    EOD_PULSE     = "eod_pulse"       # 6 PM
    WEEKLY_DIGEST = "weekly_digest"   # Sunday evening
    ON_DEMAND     = "on_demand"       # triggered by Slack query


# ── Staleness categories ─────────────────────────────────────────────────────

class StaleType(StrEnum):
    TICKET    = "stale_ticket"
    PR        = "stale_pr"
    NOTION_DOC = "stale_notion_doc"


# ── Action types (what the Action agent can do) ───────────────────────────────

class ActionType(StrEnum):
    SEND_SLACK_MESSAGE   = "send_slack_message"
    CREATE_LINEAR_TICKET = "create_linear_ticket"
    DRAFT_NOTION_UPDATE  = "draft_notion_update"
    LOG_ONLY             = "log_only"
