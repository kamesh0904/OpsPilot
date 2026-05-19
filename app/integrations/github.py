"""
app/integrations/github.py
───────────────────────────
GitHub REST API client (via httpx, no heavy SDK dependency).

Responsibilities:
  - Fetch open pull requests across configured repos
  - Enrich PRs with review state (approved / changes_requested / pending)

GitHub REST API base URL: https://api.github.com
Auth: Authorization: Bearer {token}
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.integrations.base import BaseAPIClient
from app.integrations.models import PullRequest

log = get_logger(__name__)


class GitHubClient(BaseAPIClient):
    """
    Async client for the GitHub REST API.

    Usage:
        async with GitHubClient() as client:
            prs = await client.fetch_open_pull_requests()
    """

    BASE_URL = "https://api.github.com"

    def __init__(self) -> None:
        super().__init__(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {settings.github_token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_datetime(s: Optional[str]) -> datetime:
        if not s:
            return datetime.now(timezone.utc)
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    async def _fetch_pr_reviews(self, repo: str, pr_number: int) -> Optional[str]:
        """
        Return the latest aggregate review decision for a PR:
          "APPROVED" | "CHANGES_REQUESTED" | None (pending/no reviews)
        """
        try:
            data = await self._get(f"/repos/{repo}/pulls/{pr_number}/reviews")
            if not data:
                return None

            # Walk reviews in reverse chronological order; last state wins
            decisions = [r.get("state") for r in reversed(data) if r.get("state")]
            for decision in decisions:
                if decision in ("APPROVED", "CHANGES_REQUESTED"):
                    return decision
            return None
        except Exception as exc:
            log.warning(
                "github_reviews_fetch_failed",
                repo=repo,
                pr=pr_number,
                error=str(exc),
            )
            return None

    def _parse_pr(self, raw: dict, repo: str, review_decision: Optional[str]) -> PullRequest:
        """Convert a raw GitHub PR payload into a PullRequest model."""
        return PullRequest(
            number=raw["number"],
            title=raw["title"],
            repo=repo,
            state=raw["state"],
            author=raw["user"]["login"],
            created_at=self._parse_datetime(raw.get("created_at")),
            updated_at=self._parse_datetime(raw.get("updated_at")),
            url=raw["html_url"],
            draft=raw.get("draft", False),
            requested_reviewers=[
                r["login"] for r in raw.get("requested_reviewers", [])
            ],
            review_decision=review_decision,
            base_branch=raw.get("base", {}).get("ref", "main"),
            head_branch=raw.get("head", {}).get("ref", ""),
            additions=raw.get("additions", 0),
            deletions=raw.get("deletions", 0),
        )

    # ── Public API ────────────────────────────────────────────────────────────

    async def fetch_open_pull_requests(
        self,
        repos: Optional[list[str]] = None,
        enrich_reviews: bool = True,
    ) -> list[PullRequest]:
        """
        Fetch all open PRs across the configured repos.

        Args:
            repos:           List of "org/repo" strings.
                             Defaults to settings.github_org + settings.github_repos.
            enrich_reviews:  If True, fetch review state per PR (extra API calls).
                             Set to False for faster collection if reviews aren't needed.

        Returns:
            List of PullRequest objects.
        """
        repo_list = repos or [
            f"{settings.github_org}/{r}" for r in settings.github_repos
        ]

        if not repo_list:
            log.warning("github_no_repos_configured")
            return []

        all_prs: list[PullRequest] = []

        for repo in repo_list:
            log.info("github_fetch_prs_start", repo=repo)
            try:
                page = 1
                while True:
                    raw_prs = await self._get(
                        f"/repos/{repo}/pulls",
                        params={"state": "open", "per_page": 100, "page": page},
                    )
                    if not raw_prs:
                        break

                    for raw in raw_prs:
                        review_decision = None
                        if enrich_reviews:
                            review_decision = await self._fetch_pr_reviews(
                                repo, raw["number"]
                            )
                        all_prs.append(self._parse_pr(raw, repo, review_decision))

                    if len(raw_prs) < 100:
                        break   # last page
                    page += 1

                log.info(
                    "github_fetch_prs_done",
                    repo=repo,
                    count=sum(1 for pr in all_prs if pr.repo == repo),
                )

            except Exception as exc:
                log.error("github_fetch_prs_failed", repo=repo, error=str(exc))
                # Continue with other repos rather than failing entirely

        return all_prs

    async def fetch_recent_commits(
        self,
        repo: str,
        branch: str = "main",
        limit: int = 20,
    ) -> list[dict]:
        """
        Fetch recent commit summaries for a repo branch.

        Returns:
            List of dicts: {sha, message, author, date}
        """
        log.info("github_fetch_commits", repo=f"{settings.github_org}/{repo}", branch=branch)
        full_repo = f"{settings.github_org}/{repo}"
        raw = await self._get(
            f"/repos/{full_repo}/commits",
            params={"sha": branch, "per_page": limit},
        )
        return [
            {
                "sha": c["sha"][:7],
                "message": c["commit"]["message"].split("\n")[0],  # first line only
                "author": c["commit"]["author"]["name"],
                "date": c["commit"]["author"]["date"],
            }
            for c in raw
        ]
