"""
OpsPilot — entry point.
Run with: uvicorn main:app --reload
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.logging import configure_logging, get_logger
from app.scheduler import start_scheduler, stop_scheduler

log = get_logger(__name__)

from app.api.routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Configure structured logging first — before any other startup work
    configure_logging()
    log.info("opspilot_startup", env=app.state.__dict__.get("env", "?"))

    # 2. Start the APScheduler — registers morning/eod/weekly cron jobs
    start_scheduler()

    try:
        yield  # ← server handles requests here
    finally:
        # 3. Graceful shutdown — guaranteed to run even if server body raises
        stop_scheduler()
        log.info("opspilot_shutdown")


app = FastAPI(
    title="OpsPilot",
    description="AI-powered ops co-pilot for early-stage startups.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(router, prefix="/api/v1")


@app.get("/")
async def root_redirect():
    """Redirect root path to interactive Swagger documentation."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "opspilot"}
