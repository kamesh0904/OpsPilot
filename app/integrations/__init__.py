"""
app/integrations/__init__.py
─────────────────────────────
Public API for the integrations layer.

Import clients and models from here:
    from app.integrations import LinearClient, GitHubClient, NotionClient, SlackClient
    from app.integrations.models import CollectorSnapshot, LinearTicket
"""

from app.integrations.linear import LinearClient
from app.integrations.github import GitHubClient
from app.integrations.notion import NotionClient
from app.integrations.slack import SlackClient, build_briefing_blocks
from app.integrations.models import (
    CollectorSnapshot,
    LinearTicket,
    NotionPage,
    PullRequest,
    SlackBriefingPayload,
    SlackMessage,
)

__all__ = [
    # Clients
    "LinearClient",
    "GitHubClient",
    "NotionClient",
    "SlackClient",
    "build_briefing_blocks",
    # Models
    "CollectorSnapshot",
    "LinearTicket",
    "NotionPage",
    "PullRequest",
    "SlackBriefingPayload",
    "SlackMessage",
]
