"""
app/api/routes/slack_events.py
───────────────────────────────
POST /slack/events

Handles inbound Slack slash commands and Events API webhooks.

Two Slack entry points exist:
  1. Slash commands  (/opspilot <question>)
     - Slack sends a URL-encoded form POST with a 3-second timeout
     - We must ACK immediately (return 200) then process in background
     - We update the message with the answer when the pipeline finishes

  2. Events API webhooks (app mentions, messages in channels)
     - Slack sends a JSON POST with the event payload
     - Includes a URL verification challenge on first setup

Security: Every inbound request is verified against Slack's HMAC-SHA256
signature before any processing occurs. Unsigned or stale requests are
rejected with 403 immediately.

The 3-second timeout problem:
  Slack slash commands expire if no response is sent within 3 seconds.
  OpsPilot's pipeline takes 10-30 seconds.
  Solution: Return 200 immediately with "⏳ Looking into it...", then
  run the pipeline in the background and post the real answer as a
  follow-up message in the same channel.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, Response
from pydantic import BaseModel

from app.core.config import settings
from app.core.constants import BriefingType
from app.core.logging import get_logger
from app.graph import run_pipeline
from app.integrations import SlackClient
from app.integrations.slack import SlackClient as SlackClientClass

log = get_logger(__name__)
router = APIRouter()


# ── Security helper ───────────────────────────────────────────────────────────

def _verify_slack_signature(
    body: bytes,
    timestamp: Optional[str],
    signature: Optional[str],
) -> bool:
    """
    Verify the HMAC-SHA256 signature Slack attaches to every inbound request.
    Returns True if valid, False if missing / invalid / stale.
    """
    if not timestamp or not signature:
        return False
    return SlackClientClass.verify_request_signature(
        body=body,
        timestamp=timestamp,
        signature=signature,
        signing_secret=settings.slack_signing_secret,
    )


# ── Background task: run pipeline and post answer ─────────────────────────────

async def _answer_in_channel(
    question: str,
    channel: str,
    response_url: Optional[str] = None,
) -> None:
    """
    Background task: run the ON_DEMAND pipeline and post the answer to Slack.
    Errors are caught and logged — they must not crash the background runner.
    """
    try:
        state = await run_pipeline(briefing_type=BriefingType.ON_DEMAND)

        decision = state.get("decision")
        run_errors = state.get("run_errors", [])

        # Build the answer text
        lines: list[str] = [f"🔍 *Re: {question}*\n"]
        if decision:
            lines.extend(decision.urgent_items)
            lines.extend(decision.watch_items)
            if decision.suggestion:
                lines.append(f"💡 {decision.suggestion}")
        if not lines[1:]:
            lines.append("No issues found related to your question.")
        if run_errors:
            lines.append(f"\n_⚠️ Note: {len(run_errors)} source(s) were unavailable._")

        answer_text = "\n".join(lines)

        async with SlackClient() as slack:
            await slack.send_text(text=answer_text, channel=channel)

        log.info("slack_ondemand_answered", channel=channel, run_errors=len(run_errors))

    except Exception as exc:
        log.error("slack_ondemand_failed", channel=channel, error=str(exc))
        # Best-effort: post an error message so Slack isn't silent
        try:
            async with SlackClient() as slack:
                await slack.send_text(
                    text="❌ Something went wrong processing your request. Please try again.",
                    channel=channel,
                )
        except Exception:
            pass


# ── Route: slash command ──────────────────────────────────────────────────────

@router.post(
    "/slack/events",
    summary="Receive Slack slash commands and Events API webhooks",
    tags=["Slack"],
    status_code=200,
)
async def slack_events(
    request: Request,
    background_tasks: BackgroundTasks,
    x_slack_request_timestamp: Optional[str] = Header(None),
    x_slack_signature: Optional[str] = Header(None),
) -> Response:
    """
    Single endpoint for all inbound Slack traffic.

    Handles:
    - **URL verification challenge** (one-time setup, returns challenge string)
    - **Slash commands** (/opspilot <question>) — ACKs in < 1s, answers in background
    - **Events API webhooks** (app mentions) — same background pattern

    All requests are signature-verified before processing.
    Unsigned / stale / malformed requests receive **403 Forbidden**.
    """
    raw_body = await request.body()

    # ── Step 1: Verify Slack signature ───────────────────────────────────────
    if not _verify_slack_signature(
        body=raw_body,
        timestamp=x_slack_request_timestamp,
        signature=x_slack_signature,
    ):
        log.warning("slack_signature_rejected")
        raise HTTPException(status_code=403, detail="Invalid Slack signature")

    # ── Step 2: Determine content type ───────────────────────────────────────
    content_type = request.headers.get("content-type", "")

    if "application/x-www-form-urlencoded" in content_type:
        # Slash command payload (URL-encoded form)
        return await _handle_slash_command(request, raw_body, background_tasks)
    else:
        # Events API JSON payload
        body = await request.json()
        return await _handle_events_api(body, background_tasks)


async def _handle_slash_command(
    request: Request,
    raw_body: bytes,
    background_tasks: BackgroundTasks,
) -> Response:
    """
    Handle /opspilot <question> slash commands.

    Must return within 3 seconds (Slack timeout).
    Immediate ACK: "⏳ Looking into it..."
    Background task: run pipeline, post real answer.
    """
    from urllib.parse import parse_qs

    params = parse_qs(raw_body.decode("utf-8"))
    text = params.get("text", [""])[0].strip()
    channel_id = params.get("channel_id", [""])[0]
    user_id = params.get("user_id", [""])[0]

    log.info(
        "slack_slash_command_received",
        user_id=user_id,
        channel=channel_id,
        question_preview=text[:60],
    )

    if not text:
        return Response(
            content="Please provide a question. Example: `/opspilot what's blocking payments?`",
            media_type="text/plain",
        )

    # Immediately schedule the background pipeline run
    background_tasks.add_task(_answer_in_channel, question=text, channel=channel_id)

    # ACK within 3 seconds with a placeholder message
    return Response(
        content="⏳ Looking into it... I'll post the answer here shortly.",
        media_type="text/plain",
    )


async def _handle_events_api(body: dict, background_tasks: BackgroundTasks) -> Response:
    """
    Handle Slack Events API webhook payloads.

    Supports:
    - url_verification challenge (Slack setup handshake)
    - app_mention and message events (triggers on-demand pipeline)
    """
    # URL verification challenge — one-time handshake during app setup
    if body.get("type") == "url_verification":
        challenge = body.get("challenge", "")
        log.info("slack_url_verification_challenge")
        return Response(content=challenge, media_type="text/plain")

    # Parse the event
    parsed = SlackClientClass.parse_event(body)
    if parsed is None:
        # Bot message, empty text, non-message event — silently ignore
        return Response(content="ok", media_type="text/plain")

    log.info(
        "slack_event_received",
        channel=parsed.channel,
        text_preview=parsed.text[:60],
    )

    # Trigger on-demand pipeline in background
    background_tasks.add_task(
        _answer_in_channel,
        question=parsed.text,
        channel=parsed.channel,
    )

    return Response(content="ok", media_type="text/plain")
