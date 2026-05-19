"""
app/core/logging.py
────────────────────
Structured logging setup using structlog.
Call configure_logging() once at startup (done in main.py).
Usage anywhere in the app:
    from app.core.logging import get_logger
    log = get_logger(__name__)
    log.info("data_collected", source="linear", tickets=42)
"""

import logging
import sys

# pyrefly: ignore [missing-import]
import structlog

from app.core.config import settings


def configure_logging() -> None:
    """
    Wire structlog to Python's standard logging.
    - Development → colourful, human-readable console output
    - Production  → JSON lines (ready for Datadog / GCP Logging / Loki)
    Call this exactly once, at application startup.
    """
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Shared processors applied to every log event
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.is_production:
        # JSON output — each line is a valid JSON object
        renderer = structlog.processors.JSONRenderer()
    else:
        # Pretty coloured output for local dev
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)

    # Quiet noisy third-party libraries
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger for the given module name."""
    return structlog.get_logger(name)
