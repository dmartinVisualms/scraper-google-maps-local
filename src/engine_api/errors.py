from __future__ import annotations

from typing import Optional


class RetryableHTTPError(Exception):
    """HTTP 429 / 5xx — retry_async debe reintentar."""

    def __init__(self, status: int):
        self.status = status
        super().__init__(f"Retryable HTTP {status}")


class FatalHTTPError(Exception):
    """HTTP 4xx no recuperable (400/401/403) — no reintentar."""

    def __init__(self, status: int, payload: Optional[object] = None):
        self.status = status
        self.payload = payload
        super().__init__(f"Fatal HTTP {status}")
