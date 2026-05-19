"""
app/integrations/notion.py
───────────────────────────
Notion API client (via notion-client SDK).

Responsibilities:
  - Search and list pages in the configured workspace
  - Detect stale documentation
  - Draft page content updates (Action agent use case)

We use the official notion-client SDK (which wraps the Notion REST API)
because it handles pagination, retries, and response parsing cleanly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from notion_client import AsyncClient as NotionAsyncClient

from app.core.config import settings
from app.core.logging import get_logger
from app.integrations.models import NotionPage

log = get_logger(__name__)


def _extract_title(page: dict) -> str:
    """
    Extract the plain text title from a Notion page object.
    Notion pages can have their title in different property keys.
    """
    props = page.get("properties", {})

    # Try common title property names
    for key in ("Name", "Title", "title", "name"):
        prop = props.get(key)
        if not prop:
            continue
        title_content = prop.get("title", [])
        if title_content:
            return "".join(t.get("plain_text", "") for t in title_content)

    # If no title found, use page ID as fallback
    return f"Untitled ({page.get('id', 'unknown')[:8]})"


def _parse_datetime(s: Optional[str]) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class NotionClient:
    """
    Async client for the Notion API.

    Usage:
        async with NotionClient() as client:
            pages = await client.fetch_all_pages()
    """

    def __init__(self) -> None:
        self._client: Optional[NotionAsyncClient] = None

    async def __aenter__(self) -> "NotionClient":
        self._client = NotionAsyncClient(auth=settings.notion_api_key)
        return self

    async def __aexit__(self, *args) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _parse_page(self, raw: dict) -> NotionPage:
        """Convert a raw Notion API page object into a NotionPage model."""
        last_edited_by_obj = raw.get("last_edited_by", {})
        last_edited_by = None
        if last_edited_by_obj:
            # Person object has name; bot object has bot.owner
            last_edited_by = (
                last_edited_by_obj.get("name")
                or last_edited_by_obj.get("id", "")[:8]
            )

        parent = raw.get("parent", {})
        parent_type = list(parent.keys())[0].replace("_id", "") if parent else "workspace"

        return NotionPage(
            id=raw["id"],
            title=_extract_title(raw),
            url=raw.get("url", ""),
            last_edited=_parse_datetime(raw.get("last_edited_time")),
            created_time=_parse_datetime(raw.get("created_time")),
            last_edited_by=last_edited_by,
            parent_type=parent_type,
            archived=raw.get("archived", False),
        )

    # ── Public API ────────────────────────────────────────────────────────────

    async def fetch_all_pages(self, limit: int = 100) -> list[NotionPage]:
        """
        Search for all pages in the workspace.

        Returns:
            List of NotionPage objects (excludes archived pages).

        Note:
            Notion's search API returns pages the integration has access to.
            Make sure the Notion integration is connected to the relevant pages.
        """
        if not self._client:
            raise RuntimeError("NotionClient must be used as an async context manager.")

        log.info("notion_fetch_start")
        pages: list[NotionPage] = []
        has_more = True
        cursor = None

        while has_more:
            params: dict = {"filter": {"property": "object", "value": "page"}}
            if cursor:
                params["start_cursor"] = cursor
            if limit:
                params["page_size"] = min(limit - len(pages), 100)

            response = await self._client.search(**params)  # type: ignore[arg-type]

            for result in response.get("results", []):
                if result.get("archived"):
                    continue
                try:
                    pages.append(self._parse_page(result))
                except Exception as exc:
                    log.warning(
                        "notion_page_parse_failed",
                        page_id=result.get("id"),
                        error=str(exc),
                    )

            has_more = response.get("has_more", False)
            cursor = response.get("next_cursor")

            if limit and len(pages) >= limit:
                break

        log.info("notion_fetch_done", count=len(pages))
        return pages

    async def fetch_stale_pages(self, stale_days: Optional[int] = None) -> list[NotionPage]:
        """
        Return pages that haven't been edited in `stale_days` days.

        Args:
            stale_days: Threshold in days. Defaults to settings.stale_notion_doc_days.
        """
        threshold = stale_days or settings.stale_notion_doc_days
        all_pages = await self.fetch_all_pages()
        stale = [p for p in all_pages if p.days_since_edit >= threshold]
        log.info(
            "notion_stale_pages",
            threshold_days=threshold,
            stale_count=len(stale),
            total=len(all_pages),
        )
        return stale

    async def append_to_page(self, page_id: str, markdown_text: str) -> bool:
        """
        Append a paragraph block to an existing Notion page.
        Used by the Action agent to draft Notion updates.

        Args:
            page_id:       The Notion page ID to append to.
            markdown_text: Plain text content (Notion doesn't support markdown directly).

        Returns:
            True on success, False on failure.
        """
        if not self._client:
            raise RuntimeError("NotionClient must be used as an async context manager.")

        try:
            await self._client.blocks.children.append(
                block_id=page_id,
                children=[
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [
                                {
                                    "type": "text",
                                    "text": {"content": markdown_text},
                                }
                            ]
                        },
                    }
                ],
            )
            log.info("notion_page_updated", page_id=page_id)
            return True
        except Exception as exc:
            log.error("notion_page_update_failed", page_id=page_id, error=str(exc))
            return False
