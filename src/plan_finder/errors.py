from __future__ import annotations

from datetime import datetime


class RateLimitError(RuntimeError):
    """Raised by a backend when the provider reports a usage/rate limit.

    retry_at, when known, is the local time the limit is expected to reset
    (parsed from the provider's message). The engine waits until then instead
    of using its generic fallback.
    """

    def __init__(self, message: str, retry_at: datetime | None = None) -> None:
        super().__init__(message)
        self.retry_at = retry_at
