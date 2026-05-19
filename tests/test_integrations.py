"""
tests/test_integrations.py
───────────────────────────
Unit tests for app/integrations/ — all external API calls are mocked.
No real tokens or network calls are needed.

Run with: python -m pytest tests/test_integrations.py -v
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.integrations.models import (
    CollectorSnapshot,
    LinearTicket,
    NotionPage,
    PullRequest,
    SlackMessage,
)
from app.integrations.slack import (
    SlackClient,
    build_briefing_blocks,
    build_header_block,
    build_section_block,
)


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _make_ticket(**kwargs) -> LinearTicket:
    defaults = dict(
        id="T-001",
        title="Fix login bug",
        status="In Progress",
        status_type="started",
        assignee=None,
        team_id="team-123",
        updated_at=datetime.now(timezone.utc) - timedelta(days=6),
        created_at=datetime.now(timezone.utc) - timedelta(days=10),
        url="https://linear.app/t/T-001",
        comment_count=2,
    )
    return LinearTicket(**{**defaults, **kwargs})


def _make_pr(**kwargs) -> PullRequest:
    defaults = dict(
        number=47,
        title="Auth refactor",
        repo="myorg/api",
        state="open",
        author="alice",
        created_at=datetime.now(timezone.utc) - timedelta(days=5),
        updated_at=datetime.now(timezone.utc) - timedelta(days=1),
        url="https://github.com/myorg/api/pull/47",
    )
    return PullRequest(**{**defaults, **kwargs})


def _make_notion_page(**kwargs) -> NotionPage:
    defaults = dict(
        id="page-abc",
        title="Q4 Roadmap",
        url="https://notion.so/q4-roadmap",
        last_edited=datetime.now(timezone.utc) - timedelta(days=50),
        created_time=datetime.now(timezone.utc) - timedelta(days=100),
    )
    return NotionPage(**{**defaults, **kwargs})


# ══════════════════════════════════════════════════════════════════════════════
# LinearTicket model tests
# ══════════════════════════════════════════════════════════════════════════════

class TestLinearTicket:
    def test_days_since_update(self):
        ticket = _make_ticket(
            updated_at=datetime.now(timezone.utc) - timedelta(days=6)
        )
        assert ticket.days_since_update == 6

    def test_is_unassigned_true(self):
        ticket = _make_ticket(assignee=None)
        assert ticket.is_unassigned is True

    def test_is_unassigned_false(self):
        ticket = _make_ticket(assignee="Alice")
        assert ticket.is_unassigned is False

    def test_is_active_for_started_status(self):
        ticket = _make_ticket(status_type="started")
        assert ticket.is_active is True

    def test_is_active_false_for_completed(self):
        ticket = _make_ticket(status_type="completed")
        assert ticket.is_active is False

    def test_is_active_false_for_cancelled(self):
        ticket = _make_ticket(status_type="cancelled")
        assert ticket.is_active is False


# ══════════════════════════════════════════════════════════════════════════════
# PullRequest model tests
# ══════════════════════════════════════════════════════════════════════════════

class TestPullRequest:
    def test_days_open(self):
        pr = _make_pr(created_at=datetime.now(timezone.utc) - timedelta(days=5))
        assert pr.days_open == 5

    def test_has_no_reviewer_true(self):
        pr = _make_pr(requested_reviewers=[], review_decision=None)
        assert pr.has_no_reviewer is True

    def test_has_no_reviewer_false_when_reviewers_assigned(self):
        pr = _make_pr(requested_reviewers=["bob"])
        assert pr.has_no_reviewer is False

    def test_has_no_reviewer_false_when_decision_exists(self):
        pr = _make_pr(requested_reviewers=[], review_decision="APPROVED")
        assert pr.has_no_reviewer is False

    def test_days_since_update(self):
        pr = _make_pr(updated_at=datetime.now(timezone.utc) - timedelta(days=3))
        assert pr.days_since_update == 3


# ══════════════════════════════════════════════════════════════════════════════
# NotionPage model tests
# ══════════════════════════════════════════════════════════════════════════════

class TestNotionPage:
    def test_days_since_edit(self):
        page = _make_notion_page(
            last_edited=datetime.now(timezone.utc) - timedelta(days=50)
        )
        assert page.days_since_edit == 50

    def test_is_stale_above_threshold(self):
        page = _make_notion_page(
            last_edited=datetime.now(timezone.utc) - timedelta(days=31)
        )
        assert page.is_stale is True

    def test_is_stale_below_threshold(self):
        page = _make_notion_page(
            last_edited=datetime.now(timezone.utc) - timedelta(days=10)
        )
        assert page.is_stale is False


# ══════════════════════════════════════════════════════════════════════════════
# CollectorSnapshot tests
# ══════════════════════════════════════════════════════════════════════════════

class TestCollectorSnapshot:
    def test_has_errors_false(self):
        snap = CollectorSnapshot()
        assert snap.has_errors is False

    def test_has_errors_true(self):
        snap = CollectorSnapshot(errors={"linear": "timeout"})
        assert snap.has_errors is True

    def test_empty_snapshot(self):
        snap = CollectorSnapshot()
        assert snap.linear_tickets == []
        assert snap.pull_requests == []
        assert snap.notion_pages == []

    def test_snapshot_with_data(self):
        snap = CollectorSnapshot(
            linear_tickets=[_make_ticket()],
            pull_requests=[_make_pr()],
            notion_pages=[_make_notion_page()],
        )
        assert len(snap.linear_tickets) == 1
        assert len(snap.pull_requests) == 1
        assert len(snap.notion_pages) == 1


# ══════════════════════════════════════════════════════════════════════════════
# Slack — signature verification
# ══════════════════════════════════════════════════════════════════════════════

class TestSlackSignatureVerification:
    """Test the HMAC signature verification without any network calls."""

    FAKE_SECRET = "fake-signing-secret-abc123"
    FAKE_BODY = b'{"event": {"type": "message"}}'

    def _make_valid_sig(self, body: bytes, timestamp: str) -> str:
        sig_base = f"v0:{timestamp}:{body.decode()}"
        hex_digest = hmac.new(
            self.FAKE_SECRET.encode(),
            sig_base.encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"v0={hex_digest}"

    def test_valid_signature_accepted(self):
        ts = str(int(time.time()))
        sig = self._make_valid_sig(self.FAKE_BODY, ts)
        result = SlackClient.verify_request_signature(
            self.FAKE_BODY, ts, sig, signing_secret=self.FAKE_SECRET
        )
        assert result is True

    def test_wrong_signature_rejected(self):
        ts = str(int(time.time()))
        result = SlackClient.verify_request_signature(
            self.FAKE_BODY, ts, "v0=invalidsignature", signing_secret=self.FAKE_SECRET
        )
        assert result is False

    def test_stale_timestamp_rejected(self):
        old_ts = str(int(time.time()) - 400)   # 400 seconds ago > 300s window
        sig = self._make_valid_sig(self.FAKE_BODY, old_ts)
        result = SlackClient.verify_request_signature(
            self.FAKE_BODY, old_ts, sig, signing_secret=self.FAKE_SECRET
        )
        assert result is False


# ══════════════════════════════════════════════════════════════════════════════
# Slack — event parsing
# ══════════════════════════════════════════════════════════════════════════════

class TestSlackEventParsing:
    def test_parse_valid_message(self):
        event_body = {
            "event": {
                "type": "message",
                "text": "<@UBOT> what's blocking payments?",
                "user": "U12345",
                "channel": "C99999",
                "ts": "1234567890.123456",
            }
        }
        msg = SlackClient.parse_event(event_body)
        assert msg is not None
        assert msg.text == "<@UBOT> what's blocking payments?"
        assert msg.user_id == "U12345"
        assert msg.is_mention is True

    def test_ignores_bot_messages(self):
        event_body = {
            "event": {
                "type": "message",
                "bot_id": "B00001",
                "text": "I am a bot",
                "user": "U99999",
                "channel": "C99999",
                "ts": "123.456",
            }
        }
        msg = SlackClient.parse_event(event_body)
        assert msg is None

    def test_ignores_non_message_events(self):
        event_body = {"event": {"type": "app_mention"}}
        msg = SlackClient.parse_event(event_body)
        assert msg is None

    def test_ignores_empty_text(self):
        event_body = {
            "event": {"type": "message", "text": "", "user": "U1", "channel": "C1", "ts": "1.2"}
        }
        msg = SlackClient.parse_event(event_body)
        assert msg is None


# ══════════════════════════════════════════════════════════════════════════════
# Slack — Block Kit builders
# ══════════════════════════════════════════════════════════════════════════════

class TestBlockKitBuilders:
    def test_header_block_structure(self):
        block = build_header_block("Good morning")
        assert block["type"] == "header"
        assert block["text"]["text"] == "Good morning"

    def test_section_block_structure(self):
        block = build_section_block("*Bold text*")
        assert block["type"] == "section"
        assert block["text"]["type"] == "mrkdwn"

    def test_briefing_blocks_with_all_sections(self):
        blocks = build_briefing_blocks(
            urgent_items=["PR #47 open 5 days"],
            watch_items=["3 tickets stalled"],
            shipped_items=["4 PRs merged"],
            suggestion="Assign PR #47 before standup",
        )
        # Should have: header, divider, urgent header, urgent item,
        #              divider, watch header, watch item, divider,
        #              shipped header, shipped items, divider, suggestion
        assert len(blocks) > 0
        types = [b["type"] for b in blocks]
        assert "header" in types
        assert "divider" in types
        assert "section" in types
        assert "context" in types

    def test_briefing_blocks_empty_sections_omitted(self):
        blocks = build_briefing_blocks(
            urgent_items=[],
            watch_items=[],
            shipped_items=["1 PR merged"],
        )
        texts = [
            b.get("text", {}).get("text", "")
            for b in blocks
            if b.get("type") == "section"
        ]
        # No urgent or watch sections should appear
        assert not any("URGENT" in t for t in texts)
        assert not any("WATCH" in t for t in texts)
        assert any("SHIPPED" in t for t in texts)
