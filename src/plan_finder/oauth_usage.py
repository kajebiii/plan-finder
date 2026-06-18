"""Claude OAuth `/api/oauth/usage` endpoint client.

The endpoint returns server-side utilization (%) for the active 5-hour session
and 7-day weekly windows, plus their reset timestamps. We use it as the
authoritative source for throttling instead of estimating from local logs.

Credentials are loaded from the macOS Keychain (where Claude Code stores them
on darwin) and fall back to `~/.claude/.credentials.json` on other platforms.

This endpoint is undocumented; the schema was discovered via the codexbar
project (https://github.com/steipete/codexbar). The required beta header
`anthropic-beta: oauth-2025-04-20` may change.
"""

from __future__ import annotations

import json
import platform
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

CREDS_KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDS_FILE = Path.home() / ".claude" / ".credentials.json"

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
BETA_HEADER_VALUE = "oauth-2025-04-20"
USER_AGENT = "plan-finder/0 (https://github.com/kajebiii/plan-finder)"

DEFAULT_HTTP_TIMEOUT_SECS = 15
DEFAULT_CACHE_TTL_SECS = 60


class OAuthCredentialsMissing(RuntimeError):
    """Credentials not found in Keychain or file. User must run `claude login`."""


class OAuthUnauthorized(RuntimeError):
    """HTTP 401 — token expired or invalid. User must re-authenticate."""


class OAuthRateLimited(RuntimeError):
    """HTTP 429 — back off until `retry_after` seconds."""

    def __init__(self, retry_after: int):
        super().__init__(f"Rate limited (retry after {retry_after}s)")
        self.retry_after = retry_after


class OAuthEndpointError(RuntimeError):
    """Network failure, 5xx, or otherwise unexpected response."""


@dataclass
class ClaudeCredentials:
    access_token: str
    refresh_token: str | None
    expires_at_ms: int | None
    subscription_type: str | None


@dataclass
class UsageSnapshot:
    """Subset of /api/oauth/usage response we actually use."""

    five_hour_pct: float | None
    five_hour_resets_at: datetime | None  # tz-aware UTC
    seven_day_pct: float | None
    seven_day_resets_at: datetime | None
    seven_day_opus_pct: float | None
    seven_day_sonnet_pct: float | None
    fetched_at: datetime  # tz-aware UTC


def _load_from_keychain() -> dict | None:
    """Read the credentials blob from macOS Keychain."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", CREDS_KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _load_from_file() -> dict | None:
    """Read `~/.claude/.credentials.json` (used on non-macOS or when keychain
    is unavailable)."""
    if not CREDS_FILE.exists():
        return None
    try:
        return json.loads(CREDS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def load_credentials() -> ClaudeCredentials:
    """Locate and parse stored Claude OAuth credentials.

    Tries macOS Keychain first (where Claude Code stores them on darwin),
    then falls back to the credentials file. Raises OAuthCredentialsMissing
    when neither source has usable data.
    """
    blob: dict | None = None
    if platform.system() == "Darwin":
        blob = _load_from_keychain()
    if blob is None:
        blob = _load_from_file()
    if blob is None:
        raise OAuthCredentialsMissing(
            "Could not load Claude credentials. Run `claude login`."
        )

    oauth = blob.get("claudeAiOauth")
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        raise OAuthCredentialsMissing(
            "claudeAiOauth.accessToken missing from credentials."
        )
    return ClaudeCredentials(
        access_token=oauth["accessToken"],
        refresh_token=oauth.get("refreshToken"),
        expires_at_ms=oauth.get("expiresAt"),
        subscription_type=oauth.get("subscriptionType"),
    )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def fetch_usage(
    creds: ClaudeCredentials,
    timeout: float = DEFAULT_HTTP_TIMEOUT_SECS,
) -> UsageSnapshot:
    """Make a single HTTP request to /api/oauth/usage and return the
    fields we care about. Raises one of OAuth* errors on failure."""
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {creds.access_token}",
            "anthropic-beta": BETA_HEADER_VALUE,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise OAuthUnauthorized(
                "HTTP 401 from /api/oauth/usage — token expired or invalid."
            )
        if e.code == 429:
            try:
                retry_after = int(e.headers.get("Retry-After", "60") or "60")
            except (TypeError, ValueError):
                retry_after = 60
            raise OAuthRateLimited(retry_after)
        raise OAuthEndpointError(
            f"HTTP {e.code} from /api/oauth/usage: {e.reason}"
        )
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise OAuthEndpointError(
            f"Network error contacting /api/oauth/usage: {type(e).__name__}: {e}"
        )
    except (ValueError, json.JSONDecodeError) as e:
        raise OAuthEndpointError(f"Unparseable response from /api/oauth/usage: {e}")

    def _window(key: str) -> tuple[float | None, datetime | None]:
        window = payload.get(key)
        if not isinstance(window, dict):
            return None, None
        util = window.get("utilization")
        if util is not None:
            try:
                util = float(util)
            except (TypeError, ValueError):
                util = None
        return util, _parse_iso(window.get("resets_at"))

    five_pct, five_resets = _window("five_hour")
    seven_pct, seven_resets = _window("seven_day")
    opus_pct, _ = _window("seven_day_opus")
    sonnet_pct, _ = _window("seven_day_sonnet")

    return UsageSnapshot(
        five_hour_pct=five_pct,
        five_hour_resets_at=five_resets,
        seven_day_pct=seven_pct,
        seven_day_resets_at=seven_resets,
        seven_day_opus_pct=opus_pct,
        seven_day_sonnet_pct=sonnet_pct,
        fetched_at=datetime.now(timezone.utc),
    )


class ClaudeOAuthUsage:
    """Caching wrapper around :func:`fetch_usage`.

    Plan-finder iterations typically run minutes apart, so a short in-memory
    cache prevents pointless extra HTTP requests when the throttle is checked
    multiple times in quick succession.
    """

    def __init__(self, cache_ttl_secs: float = DEFAULT_CACHE_TTL_SECS) -> None:
        self._cache_ttl_secs = cache_ttl_secs
        self._cached: UsageSnapshot | None = None

    def get(self, force: bool = False) -> UsageSnapshot:
        if not force and self._cached is not None:
            age_secs = (
                datetime.now(timezone.utc) - self._cached.fetched_at
            ).total_seconds()
            if age_secs < self._cache_ttl_secs:
                return self._cached
        creds = load_credentials()
        snapshot = fetch_usage(creds)
        self._cached = snapshot
        return snapshot
