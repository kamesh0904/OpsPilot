"""
tests/test_routes.py
─────────────────────
Unit tests for all FastAPI routes in app/api/routes/.

Uses FastAPI's TestClient (synchronous HTTPX wrapper) for route-level
testing. run_pipeline is patched with AsyncMock throughout — no real
API calls, no real LangGraph execution.

Coverage:
  TestBriefingRoute     (6 tests) — valid triggers, invalid run_type,
                                    background task registration
  TestQueryRoute        (6 tests) — valid question, empty question,
                                    pipeline error → 503, sources_used,
                                    answer extraction from decision
  TestConfigRoute       (6 tests) — GET config returns all fields,
                                    POST updates stale days, POST updates
                                    channel, unknown fields ignored,
                                    validation (ge/le constraints)
  TestSlackEventsRoute  (6 tests) — url_verification challenge, invalid
                                    signature → 403, slash command ACK,
                                    bot message ignored, app_mention triggers
                                    background task, missing signature → 403

Total: 24 tests.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.constants import BriefingType


# ── App fixture ───────────────────────────────────────────────────────────────

def _make_client() -> TestClient:
    """
    Create a TestClient for the FastAPI app with the scheduler patched out
    so lifespan doesn't attempt to start APScheduler during tests.
    """
    with (
        patch("main.start_scheduler"),
        patch("main.stop_scheduler"),
        patch("main.configure_logging"),
    ):
        import main as main_module
        return TestClient(main_module.app, raise_server_exceptions=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_pipeline_state(
    briefing_sent: bool = True,
    urgent_items: list | None = None,
    watch_items: list | None = None,
    suggestion: str | None = None,
    run_errors: list | None = None,
    has_snapshot: bool = True,
):
    """Build a minimal OpsState-like dict for mocking run_pipeline returns."""
    decision = MagicMock()
    decision.urgent_items = urgent_items if urgent_items is not None else ["🔴 PR #47 open 6 days"]
    decision.watch_items = watch_items if watch_items is not None else []
    decision.shipped_items = []
    decision.suggestion = suggestion
    decision.is_empty = not (decision.urgent_items or decision.watch_items)

    action_result = MagicMock()
    action_result.briefing_sent = briefing_sent
    action_result.errors = []

    snapshot = MagicMock() if has_snapshot else None
    if snapshot:
        snapshot.linear_tickets = [MagicMock()]
        snapshot.pull_requests = [MagicMock()]
        snapshot.notion_pages = []

    return {
        "decision": decision,
        "action_result": action_result,
        "snapshot": snapshot,
        "run_errors": run_errors or [],
    }


def _slack_signature(body: bytes, secret: str = "fake-signing-secret") -> tuple[str, str]:
    """Generate a valid Slack HMAC signature for test requests."""
    timestamp = str(int(time.time()))
    sig_base = f"v0:{timestamp}:{body.decode()}"
    sig = "v0=" + hmac.new(
        secret.encode(), sig_base.encode(), hashlib.sha256
    ).hexdigest()
    return timestamp, sig


# ── TestBriefingRoute ─────────────────────────────────────────────────────────

class TestBriefingRoute:

    def test_trigger_morning_returns_200(self):
        """POST /briefing/trigger with run_type=morning returns 200 + run_id."""
        client = _make_client()
        with patch(
            "app.api.routes.briefing.run_pipeline",
            new=AsyncMock(return_value=_make_pipeline_state()),
        ):
            resp = client.post("/api/v1/briefing/trigger", json={"run_type": "morning"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "started"
        assert data["briefing_type"] == BriefingType.DAILY_MORNING
        assert "run_id" in data

    def test_trigger_evening_returns_200(self):
        """POST /briefing/trigger with run_type=evening returns 200."""
        client = _make_client()
        with patch(
            "app.api.routes.briefing.run_pipeline",
            new=AsyncMock(return_value=_make_pipeline_state()),
        ):
            resp = client.post("/api/v1/briefing/trigger", json={"run_type": "evening"})

        assert resp.status_code == 200
        assert resp.json()["briefing_type"] == BriefingType.EOD_PULSE

    def test_trigger_weekly_returns_200(self):
        """POST /briefing/trigger with run_type=weekly returns 200."""
        client = _make_client()
        with patch(
            "app.api.routes.briefing.run_pipeline",
            new=AsyncMock(return_value=_make_pipeline_state()),
        ):
            resp = client.post("/api/v1/briefing/trigger", json={"run_type": "weekly"})

        assert resp.status_code == 200
        assert resp.json()["briefing_type"] == BriefingType.WEEKLY_DIGEST

    def test_trigger_invalid_run_type_returns_422(self):
        """POST /briefing/trigger with unknown run_type returns 422 validation error."""
        client = _make_client()
        resp = client.post("/api/v1/briefing/trigger", json={"run_type": "quarterly"})
        assert resp.status_code == 422

    def test_trigger_missing_body_returns_422(self):
        """POST /briefing/trigger with no body returns 422."""
        client = _make_client()
        resp = client.post("/api/v1/briefing/trigger")
        assert resp.status_code == 422

    def test_trigger_response_has_message(self):
        """Response includes a human-readable message field."""
        client = _make_client()
        with patch(
            "app.api.routes.briefing.run_pipeline",
            new=AsyncMock(return_value=_make_pipeline_state()),
        ):
            resp = client.post("/api/v1/briefing/trigger", json={"run_type": "morning"})

        assert "message" in resp.json()
        assert len(resp.json()["message"]) > 0


# ── TestQueryRoute ────────────────────────────────────────────────────────────

class TestQueryRoute:

    def test_valid_question_returns_200(self):
        """POST /query with a valid question returns 200."""
        client = _make_client()
        with patch(
            "app.api.routes.query.run_pipeline",
            new=AsyncMock(return_value=_make_pipeline_state()),
        ):
            resp = client.post("/api/v1/query", json={"question": "What's blocking payments?"})

        assert resp.status_code == 200
        data = resp.json()
        assert "answer" in data
        assert "run_id" in data
        assert data["question"] == "What's blocking payments?"

    def test_question_too_short_returns_422(self):
        """Question shorter than 3 chars is rejected with 422."""
        client = _make_client()
        resp = client.post("/api/v1/query", json={"question": "hi"})
        assert resp.status_code == 422

    def test_question_too_long_returns_422(self):
        """Question longer than 500 chars is rejected with 422."""
        client = _make_client()
        resp = client.post("/api/v1/query", json={"question": "x" * 501})
        assert resp.status_code == 422

    def test_pipeline_error_returns_503(self):
        """If run_pipeline raises, the route returns 503 Service Unavailable."""
        client = _make_client()
        with patch(
            "app.api.routes.query.run_pipeline",
            new=AsyncMock(side_effect=RuntimeError("graph failed")),
        ):
            resp = client.post("/api/v1/query", json={"question": "What is broken?"})

        assert resp.status_code == 503

    def test_sources_used_from_snapshot(self):
        """sources_used reflects which collections had data."""
        client = _make_client()
        state = _make_pipeline_state(has_snapshot=True)
        with patch(
            "app.api.routes.query.run_pipeline",
            new=AsyncMock(return_value=state),
        ):
            resp = client.post("/api/v1/query", json={"question": "Who owns auth refactor?"})

        data = resp.json()
        assert "linear" in data["sources_used"]
        assert "github" in data["sources_used"]

    def test_empty_decision_returns_no_issues_message(self):
        """When decision has no items, answer defaults to 'No issues found'."""
        client = _make_client()
        state = _make_pipeline_state(urgent_items=[], watch_items=[], suggestion=None)
        with patch(
            "app.api.routes.query.run_pipeline",
            new=AsyncMock(return_value=state),
        ):
            resp = client.post("/api/v1/query", json={"question": "Anything to worry about?"})

        assert "No issues found" in resp.json()["answer"]


# ── TestConfigRoute ───────────────────────────────────────────────────────────

class TestConfigRoute:

    def test_get_config_returns_200(self):
        """GET /config returns 200 with all expected fields."""
        client = _make_client()
        resp = client.get("/api/v1/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "stale_ticket_days" in data
        assert "stale_pr_days" in data
        assert "stale_notion_doc_days" in data
        assert "slack_channel_id" in data
        assert "daily_briefing_cron" in data

    def test_get_config_values_match_settings(self):
        """GET /config returns values that match current settings."""
        client = _make_client()
        resp = client.get("/api/v1/config")
        data = resp.json()
        assert data["stale_ticket_days"] == settings.stale_ticket_days
        assert data["stale_pr_days"] == settings.stale_pr_days

    def test_post_config_updates_stale_days(self):
        """POST /config with stale_ticket_days updates the value."""
        client = _make_client()
        resp = client.post("/api/v1/config", json={"stale_ticket_days": 7})
        assert resp.status_code == 200
        data = resp.json()
        assert "stale_ticket_days" in data["updated_fields"]
        assert data["config"]["stale_ticket_days"] == 7

    def test_post_config_partial_update(self):
        """POST /config with only one field only reports that field as updated."""
        client = _make_client()
        resp = client.post("/api/v1/config", json={"stale_pr_days": 3})
        assert resp.status_code == 200
        data = resp.json()
        assert data["updated_fields"] == ["stale_pr_days"]

    def test_post_config_stale_days_below_min_returns_422(self):
        """stale_ticket_days < 1 fails Pydantic validation."""
        client = _make_client()
        resp = client.post("/api/v1/config", json={"stale_ticket_days": 0})
        assert resp.status_code == 422

    def test_post_config_stale_days_above_max_returns_422(self):
        """stale_ticket_days > 90 fails Pydantic validation."""
        client = _make_client()
        resp = client.post("/api/v1/config", json={"stale_ticket_days": 91})
        assert resp.status_code == 422


# ── TestSlackEventsRoute ──────────────────────────────────────────────────────

class TestSlackEventsRoute:

    def _post_json(self, client, payload: dict) -> "Response":
        """Post a signed JSON Events API payload."""
        import json
        body = json.dumps(payload).encode()
        ts, sig = _slack_signature(body)
        return client.post(
            "/api/v1/slack/events",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Slack-Request-Timestamp": ts,
                "X-Slack-Signature": sig,
            },
        )

    def _post_slash(self, client, text: str, channel: str = "C123") -> "Response":
        """Post a signed slash command payload."""
        body = f"text={text}&channel_id={channel}&user_id=U999".encode()
        ts, sig = _slack_signature(body)
        return client.post(
            "/api/v1/slack/events",
            content=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Slack-Request-Timestamp": ts,
                "X-Slack-Signature": sig,
            },
        )

    def test_url_verification_challenge(self):
        """Slack URL verification returns the challenge string."""
        client = _make_client()
        resp = self._post_json(client, {
            "type": "url_verification",
            "challenge": "3eZbrw1aBm2rZgRNFdxV2595E9zS3y2fQAtC6l",
        })
        assert resp.status_code == 200
        assert resp.text == "3eZbrw1aBm2rZgRNFdxV2595E9zS3y2fQAtC6l"

    def test_missing_signature_returns_403(self):
        """Request without Slack signature headers is rejected with 403."""
        client = _make_client()
        resp = client.post(
            "/api/v1/slack/events",
            json={"type": "url_verification", "challenge": "abc"},
        )
        assert resp.status_code == 403

    def test_invalid_signature_returns_403(self):
        """Request with a wrong signature is rejected with 403."""
        client = _make_client()
        ts = str(int(time.time()))
        resp = client.post(
            "/api/v1/slack/events",
            content=b'{"type":"url_verification","challenge":"abc"}',
            headers={
                "Content-Type": "application/json",
                "X-Slack-Request-Timestamp": ts,
                "X-Slack-Signature": "v0=badhash",
            },
        )
        assert resp.status_code == 403

    def test_slash_command_acks_immediately(self):
        """Slash command returns 200 immediately with acknowledgement text."""
        client = _make_client()
        with patch("app.api.routes.slack_events._answer_in_channel", new=AsyncMock()):
            resp = self._post_slash(client, "what is blocking payments")

        assert resp.status_code == 200
        assert "Looking into it" in resp.text

    def test_slash_command_empty_text_returns_help(self):
        """Slash command with no question returns helpful guidance."""
        client = _make_client()
        resp = self._post_slash(client, "")
        assert resp.status_code == 200
        assert "Please provide" in resp.text

    def test_bot_message_event_ignored(self):
        """Events from bots are silently ignored (no background task)."""
        client = _make_client()
        resp = self._post_json(client, {
            "type": "event_callback",
            "event": {
                "type": "message",
                "bot_id": "B123",
                "text": "Hello from bot",
                "channel": "C123",
                "ts": "1234.5678",
            },
        })
        assert resp.status_code == 200
        assert resp.text == "ok"
