"""
app/integrations/base.py
─────────────────────────
Base async HTTP client shared by Linear and GitHub integrations.

Provides:
  - httpx.AsyncClient lifecycle (open/close)
  - Auth header injection
  - Structured request/response logging
  - Automatic retry with exponential backoff on rate limits (429) and
    transient server errors (500, 502, 503)
  - Consistent error raising
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.core.logging import get_logger

log = get_logger(__name__)

# Status codes we will retry on
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _is_retryable(exc: BaseException) -> bool:
    """Return True for network errors and retryable HTTP status codes."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return False


class BaseAPIClient:
    """
    Async HTTP client base class.

    Usage:
        async with LinearClient() as client:
            tickets = await client.fetch_tickets()

    Or without context manager (manages lifecycle manually):
        client = LinearClient()
        await client.open()
        tickets = await client.fetch_tickets()
        await client.close()
    """

    BASE_URL: str = ""                  # Override in subclass
    DEFAULT_TIMEOUT: float = 30.0       # seconds
    MAX_RETRIES: int = 3

    def __init__(
        self,
        base_url: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url or self.BASE_URL
        self._extra_headers = headers or {}
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def open(self) -> None:
        """Open the underlying httpx client. Called automatically by __aenter__."""
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                **self._extra_headers,
            },
            timeout=self._timeout,
        )

    async def close(self) -> None:
        """Close the underlying httpx client. Called automatically by __aexit__."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "BaseAPIClient":
        await self.open()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # ── Core request method ───────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> httpx.Response:
        """
        Send an HTTP request with automatic retry and structured logging.
        Raises httpx.HTTPStatusError for non-retryable 4xx responses.
        """
        if self._client is None:
            raise RuntimeError(
                f"{self.__class__.__name__} must be used as an async context "
                "manager or .open() must be called first."
            )

        log.debug(
            "http_request",
            method=method,
            url=url,
            has_body=json is not None,
        )

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.MAX_RETRIES),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        ):
            with attempt:
                response = await self._client.request(
                    method, url, json=json, params=params
                )

                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 5))
                    log.warning(
                        "rate_limited",
                        url=url,
                        retry_after=retry_after,
                        attempt=attempt.retry_state.attempt_number,
                    )
                    await asyncio.sleep(retry_after)
                    response.raise_for_status()   # triggers retry

                response.raise_for_status()

                log.debug(
                    "http_response",
                    method=method,
                    url=url,
                    status=response.status_code,
                )
                return response

        # Should never reach here (reraise=True), but satisfies type checker
        raise RuntimeError("Request failed after retries")  # pragma: no cover

    # ── Convenience wrappers ──────────────────────────────────────────────────

    async def _get(self, url: str, *, params: Optional[dict] = None) -> dict:
        resp = await self._request("GET", url, params=params)
        return resp.json()

    async def _post(self, url: str, *, json: dict) -> dict:
        resp = await self._request("POST", url, json=json)
        return resp.json()
