"""Throttle plan-finder iterations based on Claude OAuth utilization.

`oauth_usage.ClaudeOAuthUsage` asks Anthropic for the server-reported
utilization percentages of the active 5-hour session and 7-day weekly
windows. When either reaches the configured target we sleep until the
soonest reset and re-check. No user-configured budget — the server is
the source of truth.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from . import display
from .oauth_usage import (
    ClaudeOAuthUsage,
    OAuthCredentialsMissing,
    OAuthEndpointError,
    OAuthRateLimited,
    OAuthUnauthorized,
    UsageSnapshot,
)

DEFAULT_TARGET_PCT = 95.0

_MIN_WAIT_SECS = 60
_MAX_WAIT_SECS = 30 * 60


def _fmt_reset(d: datetime | None) -> str:
    """Compact display of a reset time relative to now."""
    if d is None:
        return "—"
    local = d.astimezone()
    now_local = datetime.now(local.tzinfo)
    if abs((local - now_local).total_seconds()) < 24 * 3600:
        return local.strftime("%H:%M")
    return local.strftime("%m-%d %H:%M")


class SessionThrottle:
    """Percentage-based throttle.

    Engaged when `five_hour_pct >= target_session_pct` OR
    `seven_day_pct >= target_weekly_pct`. While engaged, callers of
    `wait_if_needed()` sleep until the soonest reset and refresh.
    """

    def __init__(
        self,
        target_session_pct: float = DEFAULT_TARGET_PCT,
        target_weekly_pct: float = DEFAULT_TARGET_PCT,
        oauth_client: ClaudeOAuthUsage | None = None,
    ) -> None:
        self.target_session_pct = target_session_pct
        self.target_weekly_pct = target_weekly_pct
        self._client = oauth_client or ClaudeOAuthUsage()
        self.last_snapshot: UsageSnapshot | None = None
        self.disabled_reason: str | None = None
        # Tracked only for status_line display continuity across iterations.
        self.model: str | None = None
        # Initial attach attempt — graceful, never raises.
        self.refresh(force=True)

    # ----- Backward-compat shims (engine.py used these names) -----

    @property
    def session_ready(self) -> bool:
        return self.last_snapshot is not None

    @property
    def session_pct(self) -> float | None:
        return self.last_snapshot.five_hour_pct if self.last_snapshot else None

    @property
    def weekly_pct(self) -> float | None:
        return self.last_snapshot.seven_day_pct if self.last_snapshot else None

    @property
    def session_end(self) -> datetime | None:
        """Local-naive datetime when the 5-hour window resets, for legacy
        callers comparing against `datetime.now()`."""
        if self.last_snapshot and self.last_snapshot.five_hour_resets_at:
            return self.last_snapshot.five_hour_resets_at.astimezone().replace(
                tzinfo=None
            )
        return None

    def reinit(self) -> None:
        """Force-refresh after a session reset or transient error."""
        self.refresh(force=True)

    def add_usage(
        self, cost_usd: float, tokens: int, model: str | None = None
    ) -> None:
        """Deprecated. Server-side utilization is now the source of truth, so
        per-iteration cost accumulation is no longer needed. We keep the
        signature so existing call sites continue to work; only `model` is
        retained for the status line."""
        if model and self.model is None:
            self.model = model

    # ----- Core API -----

    def refresh(self, force: bool = False) -> bool:
        """Fetch the latest UsageSnapshot. Returns True if currently attached
        (we have a valid snapshot), False if throttle is disabled."""
        try:
            snapshot = self._client.get(force=force)
        except OAuthCredentialsMissing:
            self._disable("credentials missing — run `claude login`")
            return False
        except OAuthUnauthorized:
            self._disable("token expired — run `claude login`")
            return False
        except OAuthRateLimited as e:
            self._disable(
                f"OAuth /usage rate-limited; retry in {e.retry_after}s"
            )
            return False
        except OAuthEndpointError as e:
            self._disable(f"OAuth endpoint error: {e}")
            return False

        was_attached = self.last_snapshot is not None
        self.last_snapshot = snapshot
        self.disabled_reason = None
        if not was_attached:
            display.console.print(
                f"[dim]Throttle armed via OAuth: "
                f"session {snapshot.five_hour_pct or 0:.0f}% "
                f"(resets {_fmt_reset(snapshot.five_hour_resets_at)}), "
                f"weekly {snapshot.seven_day_pct or 0:.0f}% "
                f"(resets {_fmt_reset(snapshot.seven_day_resets_at)}).[/dim]"
            )
        return True

    def is_allowed(self) -> bool:
        """True when both session and weekly utilization are below target,
        or when throttle is disabled (graceful no-op)."""
        snapshot = self.last_snapshot
        if snapshot is None:
            return True
        if (
            snapshot.five_hour_pct is not None
            and snapshot.five_hour_pct >= self.target_session_pct
        ):
            return False
        if (
            snapshot.seven_day_pct is not None
            and snapshot.seven_day_pct >= self.target_weekly_pct
        ):
            return False
        return True

    async def wait_if_needed(self) -> None:
        """Sleep until the soonest reset of an exceeded window, refresh, repeat
        until allowed. No-op when disabled or already allowed."""
        while not self.is_allowed():
            wait_secs = self._seconds_until_next_reset()
            wait_secs = max(_MIN_WAIT_SECS, min(_MAX_WAIT_SECS, wait_secs))
            snapshot = self.last_snapshot
            now_str = datetime.now().strftime("%H:%M:%S")
            display.console.print(
                f"\n[yellow][{now_str}] Throttling: "
                f"session {snapshot.five_hour_pct or 0:.0f}% / "
                f"weekly {snapshot.seven_day_pct or 0:.0f}% reached target "
                f"({self.target_session_pct:.0f}%/{self.target_weekly_pct:.0f}%). "
                f"Sleeping {wait_secs / 60:.1f} min before re-check...[/yellow]"
            )
            await asyncio.sleep(wait_secs)
            display.console.print(
                "[dim]Throttle wait done, refreshing utilization...[/dim]"
            )
            self.refresh(force=True)

    def status_line(self) -> str:
        snapshot = self.last_snapshot
        model_str = f" | Model: {self.model}" if self.model else ""
        if snapshot is None:
            reason = self.disabled_reason or "no snapshot"
            return f"Throttle disabled ({reason}){model_str}"

        s = snapshot.five_hour_pct
        w = snapshot.seven_day_pct
        worst = max(s or 0.0, w or 0.0)
        if worst < 50:
            indicator = "🟢 Plenty"
        elif worst < 75:
            indicator = "🟡 OK"
        elif worst < 90:
            indicator = "🟠 Tight"
        else:
            indicator = "🔴 High"

        s_part = (
            f"Session {s:.0f}% (resets {_fmt_reset(snapshot.five_hour_resets_at)})"
            if s is not None
            else "Session —"
        )
        w_part = (
            f"Weekly {w:.0f}% (resets {_fmt_reset(snapshot.seven_day_resets_at)})"
            if w is not None
            else "Weekly —"
        )
        return f"{s_part} | {w_part} | {indicator}{model_str}"

    # ----- Internals -----

    def _disable(self, reason: str) -> None:
        if self.disabled_reason != reason:
            display.console.print(f"[dim]Throttle disabled: {reason}[/dim]")
        self.disabled_reason = reason
        self.last_snapshot = None

    def _seconds_until_next_reset(self) -> float:
        """Time to the soonest known reset across the two windows. Falls back
        to MAX_WAIT_SECS if the snapshot has no reset times (unusual)."""
        snapshot = self.last_snapshot
        if snapshot is None:
            return _MAX_WAIT_SECS
        now_utc = datetime.now(timezone.utc)
        candidates: list[float] = []
        for resets_at in (
            snapshot.five_hour_resets_at,
            snapshot.seven_day_resets_at,
        ):
            if resets_at is None:
                continue
            secs = (resets_at - now_utc).total_seconds()
            if secs > 0:
                candidates.append(secs)
        if not candidates:
            return _MAX_WAIT_SECS
        return min(candidates)
