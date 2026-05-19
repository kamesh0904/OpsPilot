"""
app/api/routes/config.py
─────────────────────────
GET  /config
POST /config

Read and update OpsPilot's runtime configuration.

Phase 1 scope — configuration lives in settings (environment variables).
These routes expose the *read* values and accept *overrides* for the
current process lifetime only (no database persistence in Phase 1).

What's configurable:
  - Staleness thresholds (stale_ticket_days, stale_pr_days, stale_notion_doc_days)
  - Slack channel override
  - Cron schedule expressions (read-only in Phase 1 — restart required to change)

Phase 2: These will read/write from a per-workspace config table in Supabase,
allowing multiple workspaces with independent configurations.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter()


# ── Response / Request models ─────────────────────────────────────────────────

class ConfigResponse(BaseModel):
    """Current OpsPilot configuration (read from settings)."""
    # Schedules (cron expressions — read-only via API in Phase 1)
    daily_briefing_cron: str
    eod_pulse_cron: str
    weekly_digest_cron: str

    # Staleness thresholds
    stale_ticket_days: int
    stale_pr_days: int
    stale_notion_doc_days: int

    # Slack
    slack_channel_id: str

    # Environment
    app_env: str
    gemini_model: str


class ConfigUpdateRequest(BaseModel):
    """
    Subset of settings that can be updated via the API at runtime.
    Only the fields provided in the request body are changed — omitted
    fields keep their current values.
    """
    stale_ticket_days: Optional[int] = Field(
        None, ge=1, le=90, description="Days before a ticket is flagged as stale."
    )
    stale_pr_days: Optional[int] = Field(
        None, ge=1, le=60, description="Days before a PR is flagged as stale."
    )
    stale_notion_doc_days: Optional[int] = Field(
        None, ge=1, le=365, description="Days before a Notion page is flagged as stale."
    )
    slack_channel_id: Optional[str] = Field(
        None, description="Override the default Slack channel for briefings."
    )


class ConfigUpdateResponse(BaseModel):
    """Confirmation that config was updated."""
    updated_fields: list[str]
    config: ConfigResponse


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get(
    "/config",
    response_model=ConfigResponse,
    summary="Get current OpsPilot configuration",
    tags=["Config"],
)
async def get_config() -> ConfigResponse:
    """
    Returns the current OpsPilot runtime configuration.

    Values are read from the active Settings object (loaded from `.env`
    at server startup). Changes made via `POST /config` are reflected here
    immediately but are not persisted across restarts.
    """
    return ConfigResponse(
        daily_briefing_cron=settings.daily_briefing_cron,
        eod_pulse_cron=settings.eod_pulse_cron,
        weekly_digest_cron=settings.weekly_digest_cron,
        stale_ticket_days=settings.stale_ticket_days,
        stale_pr_days=settings.stale_pr_days,
        stale_notion_doc_days=settings.stale_notion_doc_days,
        slack_channel_id=settings.slack_channel_id,
        app_env=settings.app_env,
        gemini_model=settings.gemini_model,
    )


@router.post(
    "/config",
    response_model=ConfigUpdateResponse,
    summary="Update OpsPilot configuration",
    tags=["Config"],
)
async def update_config(body: ConfigUpdateRequest) -> ConfigUpdateResponse:
    """
    Update one or more OpsPilot configuration values at runtime.

    **Phase 1 limitation:** Changes are applied to the in-process Settings
    object only. They are lost on server restart. To persist changes,
    update your `.env` file and restart the server.

    **Phase 2:** Updates will be persisted per-workspace in Supabase.
    """
    updated_fields: list[str] = []

    # Apply each provided override to the live settings object
    # _SettingsProxy delegates to get_settings() which returns the singleton
    # Settings instance; we mutate it directly for runtime overrides.
    s = settings  # reference to the proxy → underlying Settings

    if body.stale_ticket_days is not None:
        object.__setattr__(s, "stale_ticket_days", body.stale_ticket_days)
        updated_fields.append("stale_ticket_days")

    if body.stale_pr_days is not None:
        object.__setattr__(s, "stale_pr_days", body.stale_pr_days)
        updated_fields.append("stale_pr_days")

    if body.stale_notion_doc_days is not None:
        object.__setattr__(s, "stale_notion_doc_days", body.stale_notion_doc_days)
        updated_fields.append("stale_notion_doc_days")

    if body.slack_channel_id is not None:
        object.__setattr__(s, "slack_channel_id", body.slack_channel_id)
        updated_fields.append("slack_channel_id")

    log.info("config_updated", fields=updated_fields)

    return ConfigUpdateResponse(
        updated_fields=updated_fields,
        config=ConfigResponse(
            daily_briefing_cron=settings.daily_briefing_cron,
            eod_pulse_cron=settings.eod_pulse_cron,
            weekly_digest_cron=settings.weekly_digest_cron,
            stale_ticket_days=settings.stale_ticket_days,
            stale_pr_days=settings.stale_pr_days,
            stale_notion_doc_days=settings.stale_notion_doc_days,
            slack_channel_id=settings.slack_channel_id,
            app_env=settings.app_env,
            gemini_model=settings.gemini_model,
        ),
    )
