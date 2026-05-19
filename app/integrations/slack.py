"""
app/integrations/slack.py
──────────────────────────
Slack integration — sending messages and parsing inbound events.

Responsibilities:
  - Send briefing messages (with Block Kit layout)
  - Send simple text messages / alerts
  - Parse inbound Slack events (on-demand queries via @opspilot mention)
  - Verify request signatures (security — prevent spoofed webhooks)

Uses the official slack-sdk AsyncWebClient.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional

from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError

from app.core.config import settings
from app.core.logging import get_logger
from app.integrations.models import SlackBriefingPayload, SlackMessage

log = get_logger(__name__)


class SlackClient:
    """
    Async Slack client for sending messages and verifying webhooks.

    Usage:
        async with SlackClient() as client:
            await client.send_text("Hello from OpsPilot!")
    """

    def __init__(self) -> None:
        self._client: Optional[AsyncWebClient] = None

    async def __aenter__(self) -> "SlackClient":
        self._client = AsyncWebClient(token=settings.slack_bot_token)
        return self

    async def __aexit__(self, *args) -> None:
        # slack-sdk AsyncWebClient has no explicit close — it uses aiohttp internally
        self._client = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _ensure_open(self) -> AsyncWebClient:
        if self._client is None:
            raise RuntimeError("SlackClient must be used as an async context manager.")
        return self._client

    # ── Sending ───────────────────────────────────────────────────────────────

    async def send_text(
        self,
        text: str,
        channel: Optional[str] = None,
        thread_ts: Optional[str] = None,
    ) -> bool:
        """
        Send a plain text message to a channel.

        Args:
            text:      Message text
            channel:   Channel ID or name (defaults to settings.slack_channel_id)
            thread_ts: Reply in a thread if provided

        Returns:
            True on success, False on failure.
        """
        client = self._ensure_open()
        target = channel or settings.slack_channel_id

        try:
            kwargs: dict = {"channel": target, "text": text}
            if thread_ts:
                kwargs["thread_ts"] = thread_ts

            resp = await client.chat_postMessage(**kwargs)  # type: ignore[arg-type]
            log.info("slack_message_sent", channel=target, ok=resp.get("ok"))
            return bool(resp.get("ok"))

        except SlackApiError as exc:
            log.error(
                "slack_send_failed",
                channel=target,
                error=exc.response.get("error"),
            )
            return False

    async def send_briefing(self, payload: SlackBriefingPayload) -> bool:
        """
        Send a richly formatted briefing using Slack Block Kit.

        Block Kit allows headers, dividers, bullet sections, and buttons
        which make the briefing much more readable than plain text.

        Args:
            payload: SlackBriefingPayload with blocks and fallback text.

        Returns:
            True on success, False on failure.
        """
        client = self._ensure_open()

        try:
            resp = await client.chat_postMessage(
                channel=payload.channel,
                text=payload.text,    # fallback for notifications
                blocks=payload.blocks,
            )
            log.info(
                "slack_briefing_sent",
                channel=payload.channel,
                briefing_type=payload.briefing_type,
                ok=resp.get("ok"),
            )
            return bool(resp.get("ok"))

        except SlackApiError as exc:
            log.error(
                "slack_briefing_failed",
                channel=payload.channel,
                briefing_type=payload.briefing_type,
                error=exc.response.get("error"),
            )
            return False

    async def reply_in_thread(
        self,
        channel: str,
        thread_ts: str,
        text: str,
    ) -> bool:
        """
        Reply to a specific Slack thread (used for on-demand query responses).
        """
        return await self.send_text(text, channel=channel, thread_ts=thread_ts)

    # ── Receiving / parsing ───────────────────────────────────────────────────

    @staticmethod
    def parse_event(event_body: dict) -> Optional[SlackMessage]:
        """
        Parse a Slack Events API payload into a SlackMessage.

        Args:
            event_body: The full JSON body from the Slack Events API webhook.

        Returns:
            SlackMessage if this is a parseable message event, None otherwise.
        """
        event = event_body.get("event", {})

        # Only handle message events that aren't from bots
        if event.get("type") != "message":
            return None
        if event.get("bot_id") or event.get("subtype"):
            return None

        text = event.get("text", "").strip()
        if not text:
            return None

        return SlackMessage(
            channel=event.get("channel", ""),
            text=text,
            user_id=event.get("user", ""),
            timestamp=event.get("ts", ""),
            thread_ts=event.get("thread_ts"),
            is_mention="<@" in text,  # crude mention check; refined in webhook handler
        )

    # ── Security ──────────────────────────────────────────────────────────────

    @staticmethod
    def verify_request_signature(
        body: bytes,
        timestamp: str,
        signature: str,
        signing_secret: Optional[str] = None,
    ) -> bool:
        """
        Verify that an inbound request genuinely came from Slack.

        Args:
            body:           Raw request body bytes
            timestamp:      Value of X-Slack-Request-Timestamp header
            signature:      Value of X-Slack-Signature header (format: "v0=...")
            signing_secret: HMAC secret (defaults to settings.slack_signing_secret)

        Returns:
            True if the signature is valid and the request is fresh.
        """
        secret = signing_secret or settings.slack_signing_secret

        # Reject stale requests (replay attack prevention)
        try:
            if abs(time.time() - float(timestamp)) > 300:   # 5 minute window
                log.warning("slack_stale_request", timestamp=timestamp)
                return False
        except ValueError:
            return False

        sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
        expected = (
            "v0="
            + hmac.new(
                secret.encode("utf-8"),
                sig_basestring.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        )

        is_valid = hmac.compare_digest(expected, signature)
        if not is_valid:
            log.warning("slack_invalid_signature")
        return is_valid


# ── Block Kit Builders ────────────────────────────────────────────────────────
# Helper functions to build Slack Block Kit payloads for briefings.
# Used by the Action agent when assembling the final briefing message.

def build_header_block(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": text}}


def build_section_block(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def build_divider_block() -> dict:
    return {"type": "divider"}


def build_context_block(text: str) -> dict:
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": text}],
    }


def build_briefing_blocks(
    urgent_items: list[str],
    watch_items: list[str],
    shipped_items: list[str],
    suggestion: Optional[str] = None,
) -> list[dict]:
    """
    Build a complete daily briefing Block Kit payload.

    Args:
        urgent_items: List of markdown strings for 🔴 URGENT section
        watch_items:  List of markdown strings for 🟡 WATCH section
        shipped_items: List of markdown strings for ✅ SHIPPED section
        suggestion:   Optional single suggested action for the founder

    Returns:
        List of Slack block objects ready to send.
    """
    blocks: list[dict] = [
        build_header_block("🧭 OpsPilot — Daily Briefing"),
        build_divider_block(),
    ]

    if urgent_items:
        blocks.append(build_section_block("*🔴 URGENT — Needs your attention today*"))
        for item in urgent_items:
            blocks.append(build_section_block(item))
        blocks.append(build_divider_block())

    if watch_items:
        blocks.append(build_section_block("*🟡 WATCH — Monitor these*"))
        for item in watch_items:
            blocks.append(build_section_block(item))
        blocks.append(build_divider_block())

    if shipped_items:
        blocks.append(build_section_block("*✅ SHIPPED THIS WEEK*"))
        blocks.append(
            build_section_block("\n".join(f"→ {item}" for item in shipped_items))
        )
        blocks.append(build_divider_block())

    if suggestion:
        blocks.append(
            build_context_block(f"💡 *One thing I'd suggest:* {suggestion}")
        )

    return blocks
