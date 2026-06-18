"""Session-aware throttle using cost ($) from ResultMessage.total_cost_usd.

Formula:
  (cumulative_cost / session_budget) * 1.05 < (elapsed / session_duration)

Session timing auto-detected via `ccusage blocks --json`.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime

from . import display

DEFAULT_SESSION_BUDGET = 40.0  # $40 per session


class CcusageNotInstalled(RuntimeError):
    """ccusage CLI is not installed."""


class NoActiveSession(RuntimeError):
    """ccusage found no active session block."""


def detect_session() -> dict:
    """Auto-detect current session info from ccusage.

    Returns dict with keys:
      session_start: datetime (local)
      session_end: datetime (local)
      cost_usd: float (cost already spent in this session)
      models: list[str] (models used in this session)

    Raises CcusageNotInstalled if ccusage is missing.
    Raises NoActiveSession if no active block found.
    """
    # ccusage scans every JSONL transcript under ~/.claude — heavy users (many
    # GB of history) regularly cross the old 30s budget, which silently
    # disabled throttling forever. 120s gives the scan room while still
    # bounding worst-case iteration overhead.
    _CCUSAGE_TIMEOUT_SECS = 120
    try:
        json_result = subprocess.run(
            ["ccusage", "blocks", "--json", "--active"],
            capture_output=True,
            text=True,
            timeout=_CCUSAGE_TIMEOUT_SECS,
        )
    except FileNotFoundError:
        raise CcusageNotInstalled(
            "ccusage is required but not installed. Install it with: brew install ccusage"
        )
    except subprocess.TimeoutExpired:
        raise NoActiveSession(
            f"ccusage timed out ({_CCUSAGE_TIMEOUT_SECS}s). Skipping session detection."
        )

    if json_result.returncode != 0:
        raise NoActiveSession(
            f"ccusage exited with code {json_result.returncode}: {json_result.stderr.strip()[:200]}"
        )

    data = json.loads(json_result.stdout)
    active_block = None

    for block in data.get("blocks", []):
        if block.get("isActive"):
            active_block = block

    if active_block is None:
        raise NoActiveSession("No active session found via ccusage.")

    start_utc = datetime.fromisoformat(
        active_block["startTime"].replace("Z", "+00:00")
    )
    end_utc = datetime.fromisoformat(
        active_block["endTime"].replace("Z", "+00:00")
    )
    session_start = start_utc.astimezone().replace(tzinfo=None)
    session_end = end_utc.astimezone().replace(tzinfo=None)

    return {
        "session_start": session_start,
        "session_end": session_end,
        "cost_usd": active_block.get("costUSD", 0.0),
        "models": active_block.get("models", []),
    }


class SessionThrottle:
    def __init__(
        self,
        session_budget: float = DEFAULT_SESSION_BUDGET,
    ) -> None:
        self.session_budget = session_budget
        self.cumulative_cost: float = 0.0
        self.cumulative_tokens: int = 0
        self.model: str | None = None
        self._init_session()

    def _init_session(self) -> None:
        """Detect session via ccusage.

        Raises CcusageNotInstalled if ccusage is missing.
        On NoActiveSession, sets session_ready=False (throttle disabled).
        """
        try:
            session_info = detect_session()
        except NoActiveSession:
            self.session_ready = False
            display.console.print(
                "[dim]No active session yet — throttle disabled until session starts.[/dim]"
            )
            return

        self.session_ready = True
        self.session_start = session_info["session_start"]
        self.session_end = session_info["session_end"]
        self.session_duration = self.session_end - self.session_start
        self.cumulative_cost = session_info["cost_usd"]
        models = [m for m in session_info.get("models", []) if m != "<synthetic>"]
        if models and self.model is None:
            self.model = models[0]
        display.console.print(
            f"[dim]Session detected via ccusage: "
            f"{self.session_start.strftime('%H:%M')} ~ "
            f"{self.session_end.strftime('%H:%M')}, "
            f"${self.cumulative_cost:.2f}/${self.session_budget:.0f} spent[/dim]"
        )

    def reinit(self) -> None:
        """Re-detect session info (e.g. after session reset)."""
        display.console.print("[dim]Re-detecting session...[/dim]")
        self.cumulative_cost = 0.0
        self.cumulative_tokens = 0
        self._init_session()

    def try_attach(self, min_retry_interval_secs: float = 60.0) -> bool:
        """Silent retry of session detection.

        ccusage only registers an active block after the first Claude request
        lands, so a plan-finder run kicked off just before that returns no
        active block at startup and would otherwise stay disabled forever.
        Iterations call this each turn until a session shows up; it stays quiet
        on failure and logs once on success.

        Backed off by `min_retry_interval_secs` so a broken/slow ccusage
        doesn't burn the full timeout on every iteration. Returns True if a
        session was attached this call (or was already attached), False
        otherwise.
        """
        if self.session_ready:
            return True
        now = datetime.now()
        last = getattr(self, "_last_attach_attempt", None)
        if last and (now - last).total_seconds() < min_retry_interval_secs:
            return False
        self._last_attach_attempt = now
        try:
            info = detect_session()
        except NoActiveSession:
            return False
        self.session_ready = True
        self.session_start = info["session_start"]
        self.session_end = info["session_end"]
        self.session_duration = self.session_end - self.session_start
        self.cumulative_cost = info["cost_usd"]
        models = [m for m in info.get("models", []) if m != "<synthetic>"]
        if models and self.model is None:
            self.model = models[0]
        display.console.print(
            f"[dim]Session now active via ccusage: "
            f"{self.session_start.strftime('%H:%M')} ~ "
            f"{self.session_end.strftime('%H:%M')}, "
            f"${self.cumulative_cost:.2f}/${self.session_budget:.0f} spent — "
            f"throttle armed.[/dim]"
        )
        return True

    def add_usage(self, cost_usd: float, tokens: int, model: str | None = None) -> None:
        self.cumulative_cost += cost_usd
        self.cumulative_tokens += tokens
        if model and self.model is None:
            self.model = model

    def _elapsed_ratio(self) -> float:
        now = datetime.now()
        elapsed = (now - self.session_start).total_seconds()
        total = self.session_duration.total_seconds()
        return max(0.0, min(1.0, elapsed / total))

    def _usage_ratio(self) -> float:
        if self.session_budget <= 0:
            return 0.0
        return self.cumulative_cost / self.session_budget

    def is_allowed(self) -> bool:
        if not self.session_ready:
            return True
        return self._usage_ratio() * 1.05 < self._elapsed_ratio()

    def seconds_until_allowed(self) -> float:
        usage = self._usage_ratio()
        if usage <= 0:
            return 0.0
        total_secs = self.session_duration.total_seconds()
        elapsed_secs = (datetime.now() - self.session_start).total_seconds()
        needed_elapsed = usage * 1.05 * total_secs
        remaining = max(0.0, needed_elapsed - elapsed_secs)
        # Cap at session end — session resets after that
        time_until_session_end = max(0.0, total_secs - elapsed_secs)
        return min(remaining, time_until_session_end)

    async def wait_if_needed(self) -> None:
        import asyncio

        while not self.is_allowed():
            wait = self.seconds_until_allowed()
            if wait <= 0:
                break
            wait += 30  # buffer to avoid re-triggering
            from datetime import datetime

            now_str = datetime.now().strftime("%H:%M:%S")
            display.console.print(
                f"\n[yellow][{now_str}] Throttling: cost {self._usage_ratio():.0%} * 1.05 "
                f"> time {self._elapsed_ratio():.0%}. "
                f"Waiting {wait / 60:.1f} min...[/yellow]"
            )
            await asyncio.sleep(wait)
            display.console.print("[dim]Throttle wait done, resuming...[/dim]")

    def status_line(self) -> str:
        if not self.session_ready:
            model_str = f" | Model: {self.model}" if self.model else ""
            return f"No active session — throttle disabled{model_str}"

        usage = self._usage_ratio()
        elapsed = self._elapsed_ratio()
        pace = usage * 1.05
        margin = elapsed - pace

        if margin > 0.15:
            indicator = "🟢 Plenty"
        elif margin > 0.05:
            indicator = "🟡 OK"
        elif margin > 0:
            indicator = "🟠 Tight"
        else:
            indicator = "🔴 Over"

        remaining_hours = (
            self.session_duration.total_seconds() * (1 - elapsed) / 3600
        )

        model_str = f" | Model: {self.model}" if self.model else ""

        return (
            f"Cost: ${self.cumulative_cost:.2f}/"
            f"${self.session_budget:.0f} "
            f"({usage:.0%}) | "
            f"Session: {elapsed:.0%} ({remaining_hours:.1f}h left) | "
            f"{indicator} (pace {pace:.0%} vs time {elapsed:.0%})"
            f"{model_str}"
        )
