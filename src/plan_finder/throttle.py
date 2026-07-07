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

# Window durations Anthropic uses for the OAuth utilization endpoint.
_SESSION_WINDOW_SECS = 5 * 60 * 60
_WEEKLY_WINDOW_SECS = 7 * 24 * 60 * 60

# Time-proportional pacing: keep `usage_pct * 1.05` below `elapsed_pct` so the
# budget is spent roughly evenly across the window. Matches the original
# ccusage-era formula.
_PACE_MARGIN = 1.05

_MIN_WAIT_SECS = 60
_MAX_WAIT_SECS = 30 * 60


def _seconds_until_stop_at(stop_at: object) -> float | None:
    """Return seconds from now until the next occurrence of ``stop_at`` (a
    ``datetime.time``), or None if the input has no ``hour``/``minute``.

    Assumes ``stop_at`` is in the machine's local timezone. If the time has
    already passed today the caller treats that as "already stopped" and
    should not reach this helper (throttle.wait_if_needed checks first).
    """
    hour = getattr(stop_at, "hour", None)
    minute = getattr(stop_at, "minute", None)
    if hour is None or minute is None:
        return None
    now = datetime.now()
    stop_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if stop_dt <= now:
        return 0.0
    return (stop_dt - now).total_seconds()


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
        disable_weekly: bool = False,
        disable_session: bool = False,
        oauth_client: ClaudeOAuthUsage | None = None,
    ) -> None:
        self.target_session_pct = target_session_pct
        self.target_weekly_pct = target_weekly_pct
        # When True, the 7-day window is skipped from both allow checks and
        # the status line so the throttle only paces the 5-hour session.
        # Useful when a user wants to grind through a project early in the
        # week without weekly time-proportional pacing blocking them.
        self.disable_weekly = disable_weekly
        # Symmetric to disable_weekly: when True, the 5-hour session window
        # is skipped so the throttle only paces the 7-day weekly window.
        # Useful for overnight/scheduled runs that don't care about the
        # session-level rate limit but still want to respect weekly caps.
        self.disable_session = disable_session
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
        """Allowed when neither window has reached its hard target AND each
        window's utilization is paced under its elapsed share of time.

        Pacing per window:
            usage_pct * _PACE_MARGIN <= elapsed_pct

        which is the original ccusage-era formula carried over verbatim.
        """
        snapshot = self.last_snapshot
        if snapshot is None:
            return True
        if not self.disable_session and not self._window_allowed(
            snapshot.five_hour_pct,
            snapshot.five_hour_resets_at,
            _SESSION_WINDOW_SECS,
            self.target_session_pct,
        ):
            return False
        if self.disable_weekly:
            return True
        return self._window_allowed(
            snapshot.seven_day_pct,
            snapshot.seven_day_resets_at,
            _WEEKLY_WINDOW_SECS,
            self.target_weekly_pct,
        )

    async def wait_if_needed(self, stop_at: object | None = None) -> None:
        """Sleep until the throttle re-opens, then refresh.

        ``stop_at`` (``datetime.time`` or None): if the wall clock passes this
        local time while we are still throttled, return early so the engine's
        outer loop can honor ``--stop-at`` instead of blocking indefinitely.
        Without this, a wait for a weekly reset 24h+ away would keep the
        daemon parked past ``--stop-at`` for the entire remaining window
        (observed 07-07: the daemon sat in throttle wait 8+ hours past the
        07:30 stop time until it was manually killed).
        """
        while not self.is_allowed():
            if stop_at is not None and datetime.now().time() >= stop_at:
                display.console.print(
                    f"\n[yellow]Reached stop time "
                    f"({stop_at.strftime('%H:%M')}) during throttle wait. "
                    f"Yielding to the engine.[/yellow]"
                )
                return
            wait_secs = self._seconds_until_allowed()
            wait_secs = max(_MIN_WAIT_SECS, min(_MAX_WAIT_SECS, wait_secs))
            # If ``stop_at`` is closer than the throttle's own re-check
            # interval, clamp the sleep so we wake up exactly at ``stop_at``
            # instead of overshooting and detecting the stop time only after
            # the next scheduled refresh.
            if stop_at is not None:
                secs_until_stop = _seconds_until_stop_at(stop_at)
                if secs_until_stop is not None:
                    wait_secs = min(wait_secs, secs_until_stop)
            snapshot = self.last_snapshot
            now_str = datetime.now().strftime("%H:%M:%S")
            s_pct = snapshot.five_hour_pct or 0
            w_pct = snapshot.seven_day_pct or 0
            s_elapsed = self._window_elapsed_pct(
                snapshot.five_hour_resets_at, _SESSION_WINDOW_SECS
            )
            w_elapsed = self._window_elapsed_pct(
                snapshot.seven_day_resets_at, _WEEKLY_WINDOW_SECS
            )
            display.console.print(
                f"\n[yellow][{now_str}] Throttling: "
                f"session {s_pct:.0f}% (pace {s_pct * _PACE_MARGIN:.0f}% vs "
                f"time {s_elapsed:.0f}%), "
                f"weekly {w_pct:.0f}% (pace {w_pct * _PACE_MARGIN:.0f}% vs "
                f"time {w_elapsed:.0f}%). "
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

        s_pct = snapshot.five_hour_pct
        w_pct = snapshot.seven_day_pct
        s_elapsed = self._window_elapsed_pct(
            snapshot.five_hour_resets_at, _SESSION_WINDOW_SECS
        )
        w_elapsed = self._window_elapsed_pct(
            snapshot.seven_day_resets_at, _WEEKLY_WINDOW_SECS
        )

        # Indicator based on the worst (smallest, most negative) margin
        # between elapsed_pct and usage_pct * 1.05 across the windows we
        # actually enforce. Skip either window when it's been disabled.
        margins = []
        if (
            not self.disable_session
            and s_pct is not None
            and snapshot.five_hour_resets_at is not None
        ):
            margins.append(s_elapsed - s_pct * _PACE_MARGIN)
        if (
            not self.disable_weekly
            and w_pct is not None
            and snapshot.seven_day_resets_at is not None
        ):
            margins.append(w_elapsed - w_pct * _PACE_MARGIN)
        worst_margin = min(margins) if margins else 100.0
        if worst_margin > 15:
            indicator = "🟢 Plenty"
        elif worst_margin > 5:
            indicator = "🟡 OK"
        elif worst_margin > 0:
            indicator = "🟠 Tight"
        else:
            indicator = "🔴 Over"

        if self.disable_session:
            s_part = "Session: disabled"
        elif s_pct is not None:
            s_part = (
                f"Session {s_pct:.0f}% / time {s_elapsed:.0f}% "
                f"(resets {_fmt_reset(snapshot.five_hour_resets_at)})"
            )
        else:
            s_part = "Session —"
        if self.disable_weekly:
            w_part = "Weekly: disabled"
        elif w_pct is not None:
            w_part = (
                f"Weekly {w_pct:.0f}% / time {w_elapsed:.0f}% "
                f"(resets {_fmt_reset(snapshot.seven_day_resets_at)})"
            )
        else:
            w_part = "Weekly —"
        return f"{s_part} | {w_part} | {indicator}{model_str}"

    # ----- Internals -----

    def _disable(self, reason: str) -> None:
        if self.disabled_reason != reason:
            display.console.print(f"[dim]Throttle disabled: {reason}[/dim]")
        self.disabled_reason = reason
        self.last_snapshot = None

    @staticmethod
    def _window_elapsed_pct(
        resets_at: datetime | None, duration_secs: int
    ) -> float:
        """Elapsed share of a window (0..100) given when it resets."""
        if resets_at is None:
            return 0.0
        now_utc = datetime.now(timezone.utc)
        secs_to_reset = (resets_at - now_utc).total_seconds()
        elapsed = duration_secs - max(0.0, secs_to_reset)
        return max(0.0, min(100.0, elapsed / duration_secs * 100))

    @classmethod
    def _window_allowed(
        cls,
        pct: float | None,
        resets_at: datetime | None,
        duration_secs: int,
        target_pct: float,
    ) -> bool:
        """A single window passes if it's below its hard target AND its
        usage_pct * _PACE_MARGIN does not outpace its elapsed time share."""
        if pct is None:
            return True
        if pct >= target_pct:
            return False
        if resets_at is None:
            return True  # cannot compute pacing without a reset time
        elapsed_pct = cls._window_elapsed_pct(resets_at, duration_secs)
        return pct * _PACE_MARGIN <= elapsed_pct

    def _seconds_until_allowed(self) -> float:
        """Time until the throttle re-opens.

        For each window that currently blocks us, compute when it stops
        blocking (either hard target retires at reset, or pace catches up at
        `usage_pct * _PACE_MARGIN`% elapsed) and take the maximum — we need
        every blocked window to clear before we can proceed.
        """
        snapshot = self.last_snapshot
        if snapshot is None:
            return _MIN_WAIT_SECS
        waits: list[float] = [0.0]
        windows: list[tuple[float | None, datetime | None, int, float]] = []
        if not self.disable_session:
            windows.append(
                (
                    snapshot.five_hour_pct,
                    snapshot.five_hour_resets_at,
                    _SESSION_WINDOW_SECS,
                    self.target_session_pct,
                )
            )
        if not self.disable_weekly:
            windows.append(
                (
                    snapshot.seven_day_pct,
                    snapshot.seven_day_resets_at,
                    _WEEKLY_WINDOW_SECS,
                    self.target_weekly_pct,
                )
            )
        for pct, resets_at, duration, target in windows:
            if pct is None or resets_at is None:
                continue
            secs_to_reset = max(
                0.0, (resets_at - datetime.now(timezone.utc)).total_seconds()
            )
            if pct >= target:
                # Hard cap — only reset clears it.
                waits.append(secs_to_reset)
                continue
            elapsed_pct = self._window_elapsed_pct(resets_at, duration)
            paced_pct = pct * _PACE_MARGIN
            if paced_pct <= elapsed_pct:
                continue  # not blocking
            # Need elapsed_pct to reach paced_pct.
            needed_elapsed_secs = (paced_pct / 100.0) * duration
            current_elapsed_secs = duration - secs_to_reset
            waits.append(
                min(needed_elapsed_secs - current_elapsed_secs, secs_to_reset)
            )
        return max(waits)
