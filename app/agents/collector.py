"""
app/agents/collector.py
────────────────────────
Collector Agent — "Go get the data."

Responsibilities:
  - Call all four integrations in parallel (Linear, GitHub, Notion, Slack)
  - Catch per-integration errors so one failure doesn't stop the others
  - Return a CollectorSnapshot with everything the Analyst needs

This agent does NO reasoning — it just fetches and structures data.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from app.core.config import settings
from app.core.constants import DataSource
from app.core.logging import get_logger
from app.integrations import (
    CollectorSnapshot,
    GitHubClient,
    LinearClient,
    NotionClient,
)

log = get_logger(__name__)


class CollectorAgent:
    """
    Fetches data from all configured integrations concurrently.

    Usage:
        agent = CollectorAgent()
        snapshot = await agent.run()
    """

    async def run(
        self,
        briefing_type: Optional[str] = None,
        enrich_pr_reviews: bool = True,
    ) -> CollectorSnapshot:
        """
        Run all integrations in parallel and return a unified snapshot.

        Args:
            briefing_type:      Used for logging context only.
            enrich_pr_reviews:  Pass False for faster EOD pulse runs.

        Returns:
            CollectorSnapshot — always returned, even if some integrations fail.
            Failed integrations appear in snapshot.errors.
        """
        log.info("collector_start", briefing_type=briefing_type)

        # Run all integrations concurrently
        results = await asyncio.gather(
            self._fetch_linear(),
            self._fetch_github(enrich_reviews=enrich_pr_reviews),
            self._fetch_notion(),
            return_exceptions=True,   # don't let one failure kill the others
        )

        linear_result, github_result, notion_result = results

        # Unpack results — each is either data or an exception
        tickets, errors = [], {}

        if isinstance(linear_result, Exception):
            errors[DataSource.LINEAR] = str(linear_result)
            log.error("collector_linear_failed", error=str(linear_result))
        else:
            tickets = linear_result

        prs = []
        if isinstance(github_result, Exception):
            errors[DataSource.GITHUB] = str(github_result)
            log.error("collector_github_failed", error=str(github_result))
        else:
            prs = github_result

        pages = []
        if isinstance(notion_result, Exception):
            errors[DataSource.NOTION] = str(notion_result)
            log.error("collector_notion_failed", error=str(notion_result))
        else:
            pages = notion_result

        snapshot = CollectorSnapshot(
            linear_tickets=tickets,
            pull_requests=prs,
            notion_pages=pages,
            errors=errors,
        )

        log.info(
            "collector_done",
            tickets=len(snapshot.linear_tickets),
            prs=len(snapshot.pull_requests),
            pages=len(snapshot.notion_pages),
            errors=list(snapshot.errors.keys()),
        )

        return snapshot

    # ── Private fetch methods (each wraps one integration client) ─────────────

    async def _fetch_linear(self) -> list:
        async with LinearClient() as client:
            return await client.fetch_open_tickets()

    async def _fetch_github(self, enrich_reviews: bool = True) -> list:
        async with GitHubClient() as client:
            return await client.fetch_open_pull_requests(
                enrich_reviews=enrich_reviews
            )

    async def _fetch_notion(self) -> list:
        async with NotionClient() as client:
            return await client.fetch_all_pages()
