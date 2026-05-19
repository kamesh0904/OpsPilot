"""
app/core/config.py
──────────────────
Central settings for OpsPilot.
All values are read from the environment (or .env file) via pydantic-settings.
Usage anywhere in the app:
    from app.core.config import settings
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Single source of truth for every environment variable OpsPilot needs.
    Add new variables here — never read os.environ directly in application code.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",          # silently ignore unknown env vars
    )

    # ── App ──────────────────────────────────────────────────────────────
    app_env: Literal["development", "staging", "production"] = "development"
    secret_key: str = Field(..., description="Random secret used for signing tokens")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # ── Database ─────────────────────────────────────────────────────────
    database_url: str = Field(
        ...,
        description="Async PostgreSQL DSN, e.g. postgresql+asyncpg://user:pw@host/db",
    )

    # ── Gemini / LLM ─────────────────────────────────────────────────────
    google_api_key: str = Field(..., description="Google AI Studio API key")
    gemini_model: str = "gemini-1.5-pro"

    # ── Slack ─────────────────────────────────────────────────────────────
    slack_bot_token: str = Field(..., description="xoxb-... bot token")
    slack_signing_secret: str = Field(..., description="Used to verify Slack requests")
    slack_channel_id: str = Field(
        "#ops-pilot", description="Default channel for briefings"
    )

    # ── Linear ───────────────────────────────────────────────────────────
    linear_api_key: str = Field(..., description="lin_api_... token")
    linear_team_id: str = Field(..., description="Team ID to scope Linear queries")

    # ── Notion ───────────────────────────────────────────────────────────
    notion_api_key: str = Field(..., description="secret_... integration token")
    notion_workspace_id: str = Field(..., description="Target Notion workspace")

    # ── GitHub ───────────────────────────────────────────────────────────
    github_token: str = Field(..., description="ghp_... personal / app token")
    github_org: str = Field(..., description="Org or username that owns the repos")
    github_repos_raw: str = Field(
        "", alias="github_repos", description="Comma-separated repo names"
    )

    @field_validator("github_repos_raw", mode="before")
    @classmethod
    def _default_repos(cls, v: str) -> str:
        return v or ""

    @property
    def github_repos(self) -> list[str]:
        """Parsed list of repo names from the comma-separated env var."""
        return [r.strip() for r in self.github_repos_raw.split(",") if r.strip()]

    # ── Scheduler (cron expressions) ─────────────────────────────────────
    daily_briefing_cron: str = "0 9 * * *"    # 09:00 every day
    eod_pulse_cron: str = "0 18 * * *"        # 18:00 every day
    weekly_digest_cron: str = "0 18 * * 0"    # 18:00 every Sunday

    # ── Staleness thresholds (days) ──────────────────────────────────────
    stale_ticket_days: int = 5
    stale_pr_days: int = 4
    stale_notion_doc_days: int = 30

    # ── Derived helpers ──────────────────────────────────────────────────
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Returns a cached Settings singleton.
    Reads .env exactly once per process — cheap to call anywhere.

    Override in tests via environment variables or by calling
    get_settings.cache_clear() before re-importing.
    """
    return Settings()


def get_settings_dep() -> Settings:  # FastAPI dependency
    return get_settings()


# Convenience alias — import this in application code:
#   from app.core.config import settings
#
# NOTE: This is intentionally a property-style lazy reference so that
# the module can be imported in tests without a real .env present.
# In production the first call to settings triggers .env loading.
class _SettingsProxy:
    """Transparent proxy that defers Settings instantiation until first access."""

    def __getattr__(self, name: str):
        return getattr(get_settings(), name)


settings: Settings = _SettingsProxy()  # type: ignore[assignment]
