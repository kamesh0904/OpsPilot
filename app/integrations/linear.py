"""
app/integrations/linear.py
───────────────────────────
Linear API client (GraphQL).

Responsibilities:
  - Fetch all open tickets for a team
  - Create a ticket (Action agent use case)

Linear uses a GraphQL API at https://api.linear.app/graphql.
All queries go to the same endpoint via POST with a JSON body
containing { query: "...", variables: {...} }.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.integrations.base import BaseAPIClient
from app.integrations.models import LinearTicket

log = get_logger(__name__)

LINEAR_API_URL = "https://api.linear.app/graphql"

# ── GraphQL Queries ───────────────────────────────────────────────────────────

_FETCH_ISSUES_QUERY = """
query FetchIssues($teamId: String!, $first: Int!) {
  issues(
    filter: {
      team: { id: { eq: $teamId } }
      state: { type: { nin: ["completed", "cancelled"] } }
    }
    first: $first
    orderBy: updatedAt
  ) {
    nodes {
      id
      title
      url
      priority
      createdAt
      updatedAt
      state {
        name
        type
      }
      assignee {
        name
      }
      team {
        id
      }
      comments {
        totalCount
      }
      labels {
        nodes {
          name
        }
      }
    }
  }
}
"""

_CREATE_ISSUE_MUTATION = """
mutation CreateIssue($teamId: String!, $title: String!, $description: String!) {
  issueCreate(input: {
    teamId: $teamId
    title: $title
    description: $description
  }) {
    success
    issue {
      id
      url
    }
  }
}
"""


# ── Client ────────────────────────────────────────────────────────────────────

class LinearClient(BaseAPIClient):
    """
    Async client for the Linear GraphQL API.

    Usage:
        async with LinearClient() as client:
            tickets = await client.fetch_open_tickets()
    """

    BASE_URL = "https://api.linear.app"

    def __init__(self) -> None:
        super().__init__(
            base_url=self.BASE_URL,
            headers={"Authorization": settings.linear_api_key},
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _graphql(self, query: str, variables: Optional[dict] = None) -> dict:
        """Execute a GraphQL operation and return the data payload."""
        body: dict[str, Any] = {"query": query}
        if variables:
            body["variables"] = variables

        result = await self._post("/graphql", json=body)

        if "errors" in result:
            errors = result["errors"]
            log.error("linear_graphql_error", errors=errors)
            raise RuntimeError(f"Linear GraphQL error: {errors}")

        return result.get("data", {})

    @staticmethod
    def _parse_ticket(node: dict) -> LinearTicket:
        """Convert a raw GraphQL issue node into a LinearTicket model."""
        return LinearTicket(
            id=node["id"],
            title=node["title"],
            url=node["url"],
            priority=node.get("priority", 0),
            status=node["state"]["name"],
            status_type=node["state"]["type"],
            assignee=node["assignee"]["name"] if node.get("assignee") else None,
            team_id=node["team"]["id"],
            created_at=datetime.fromisoformat(
                node["createdAt"].replace("Z", "+00:00")
            ),
            updated_at=datetime.fromisoformat(
                node["updatedAt"].replace("Z", "+00:00")
            ),
            comment_count=node["comments"]["totalCount"],
            labels=[lbl["name"] for lbl in node.get("labels", {}).get("nodes", [])],
        )

    # ── Public API ────────────────────────────────────────────────────────────

    async def fetch_open_tickets(
        self,
        team_id: Optional[str] = None,
        limit: int = 250,
    ) -> list[LinearTicket]:
        """
        Fetch all non-completed, non-cancelled tickets for a team.

        Args:
            team_id: Linear team ID (defaults to settings.linear_team_id)
            limit:   Max number of issues to fetch in one call (max 250 per Linear)

        Returns:
            List of LinearTicket objects, sorted by most recently updated.
        """
        tid = team_id or settings.linear_team_id
        log.info("linear_fetch_start", team_id=tid, limit=limit)

        data = await self._graphql(
            _FETCH_ISSUES_QUERY,
            variables={"teamId": tid, "first": limit},
        )

        nodes = data.get("issues", {}).get("nodes", [])
        tickets = [self._parse_ticket(n) for n in nodes]

        log.info("linear_fetch_done", team_id=tid, count=len(tickets))
        return tickets

    async def create_ticket(
        self,
        title: str,
        description: str,
        team_id: Optional[str] = None,
    ) -> dict:
        """
        Create a new Linear ticket.

        Args:
            title:       Issue title
            description: Markdown body (supports Linear markdown)
            team_id:     Team to create the ticket in (defaults to settings)

        Returns:
            Dict with keys: success (bool), id (str), url (str)
        """
        tid = team_id or settings.linear_team_id
        log.info("linear_create_ticket", team_id=tid, title=title)

        data = await self._graphql(
            _CREATE_ISSUE_MUTATION,
            variables={"teamId": tid, "title": title, "description": description},
        )

        result = data.get("issueCreate", {})
        if not result.get("success"):
            raise RuntimeError(f"Linear ticket creation failed for title: '{title}'")

        issue = result.get("issue", {})
        log.info("linear_ticket_created", id=issue.get("id"), url=issue.get("url"))
        return {
            "success": True,
            "id": issue.get("id"),
            "url": issue.get("url"),
        }
