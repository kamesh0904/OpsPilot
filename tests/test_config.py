"""
tests/test_config.py
─────────────────────
Verifies that the Settings model loads and validates correctly.
Run with: pytest tests/test_config.py -v
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.constants import AgentName, BriefingType, DataSource, Severity


# ── Settings loading ────────────────────────────────────────────────────────

MINIMAL_ENV = dict(
    secret_key="test-secret",
    database_url="postgresql+asyncpg://u:p@localhost/test",
    google_api_key="fake-google-key",
    slack_bot_token="xoxb-fake",
    slack_signing_secret="fake-signing",
    linear_api_key="lin_api_fake",
    linear_team_id="team-123",
    notion_api_key="secret_fake",
    notion_workspace_id="ws-123",
    github_token="ghp_fake",
    github_org="test-org",
)


def test_settings_load_minimal():
    """Settings should instantiate with the minimum required fields."""
    s = Settings(**MINIMAL_ENV)
    assert s.app_env == "development"
    assert s.gemini_model == "gemini-1.5-pro"
    assert s.stale_ticket_days == 5
    assert s.stale_pr_days == 4


def test_github_repos_parsed():
    """github_repos property should split the comma-separated string."""
    s = Settings(**{**MINIMAL_ENV, "github_repos": "repo-a,repo-b, repo-c"})
    assert s.github_repos == ["repo-a", "repo-b", "repo-c"]


def test_github_repos_empty():
    """Empty github_repos string should return an empty list."""
    s = Settings(**{**MINIMAL_ENV, "github_repos": ""})
    assert s.github_repos == []


def test_is_production_flag():
    s = Settings(**{**MINIMAL_ENV, "app_env": "production"})
    assert s.is_production is True
    assert s.is_development is False


def test_is_development_flag():
    s = Settings(**MINIMAL_ENV)
    assert s.is_development is True
    assert s.is_production is False


def test_invalid_app_env_raises():
    """An unrecognised app_env value should raise a ValidationError."""
    with pytest.raises(ValidationError):
        Settings(**{**MINIMAL_ENV, "app_env": "nonsense"})


def test_invalid_log_level_raises():
    with pytest.raises(ValidationError):
        Settings(**{**MINIMAL_ENV, "log_level": "VERBOSE"})


# ── Constants / Enums ────────────────────────────────────────────────────────

def test_agent_name_values():
    assert AgentName.COLLECTOR == "collector"
    assert AgentName.ANALYST   == "analyst"
    assert AgentName.DECISION  == "decision"
    assert AgentName.ACTION    == "action"


def test_severity_values():
    assert Severity.URGENT == "urgent"
    assert Severity.WATCH  == "watch"
    assert Severity.INFO   == "info"


def test_briefing_type_values():
    assert BriefingType.DAILY_MORNING == "daily_morning"
    assert BriefingType.ON_DEMAND     == "on_demand"


def test_data_source_values():
    assert DataSource.LINEAR == "linear"
    assert DataSource.GITHUB == "github"
