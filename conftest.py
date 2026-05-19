"""
conftest.py
────────────
Pytest session fixtures shared across all test modules.

Sets up a fake environment so the Settings proxy resolves without a real .env.
Every test that imports from app.* gets these values automatically.
"""

import os
import pytest

# Set all required env vars BEFORE any app module is imported.
# This runs at collection time, before test execution.
_TEST_ENV = {
    "APP_ENV": "development",
    "SECRET_KEY": "test-secret-key",
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/test",
    "GOOGLE_API_KEY": "fake-google-key",
    "GEMINI_MODEL": "gemini-1.5-pro",
    "SLACK_BOT_TOKEN": "xoxb-fake",
    "SLACK_SIGNING_SECRET": "fake-signing-secret",
    "SLACK_CHANNEL_ID": "#test-channel",
    "LINEAR_API_KEY": "lin_api_fake",
    "LINEAR_TEAM_ID": "team-123",
    "NOTION_API_KEY": "secret_fake",
    "NOTION_WORKSPACE_ID": "ws-123",
    "GITHUB_TOKEN": "ghp_fake",
    "GITHUB_ORG": "test-org",
    "GITHUB_REPOS": "repo-a,repo-b",
    "STALE_TICKET_DAYS": "5",
    "STALE_PR_DAYS": "4",
    "STALE_NOTION_DOC_DAYS": "30",
    "LOG_LEVEL": "ERROR",   # suppress logs during tests
}

for key, val in _TEST_ENV.items():
    os.environ.setdefault(key, val)

# Clear the settings cache so the new env vars are picked up
from app.core.config import get_settings
get_settings.cache_clear()
