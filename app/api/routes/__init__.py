"""
app/api/routes/__init__.py
──────────────────────────
Main API router — assembles all route modules into one router
that main.py mounts at /api/v1.
"""

from fastapi import APIRouter

from app.api.routes import briefing, config, query, slack_events

router = APIRouter()

router.include_router(briefing.router,      tags=["Briefing"])
router.include_router(query.router,         tags=["Query"])
router.include_router(config.router,        tags=["Config"])
router.include_router(slack_events.router,  tags=["Slack"])
