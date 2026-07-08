from __future__ import annotations

import asyncio
import re
from pathlib import Path

from rich.markup import escape as rich_escape

from . import display
from .errors import RateLimitError
from .prompts import build_prompt
from .reporter import save_plan
from .state import StateManager
from .throttle import SessionThrottle


def _resolve_backend(backend: str):
    """Return the discover_plan coroutine for the selected backend."""
    if backend == "codex":
        from .codex_discovery import discover_plan
        return discover_plan
    from .discovery import discover_plan
    return discover_plan


QUIET_START = 22  # 22:00
QUIET_END = 3     # 03:00

# Errors that indicate rate limit / session exhaustion. Only consulted
# for errors that do NOT carry an explicit HTTP status (legacy SDK wraps,
# CLI text without api_error_status). When discovery surfaces an HTTP
# status, _http_status_from_error() routes purely by code — keeping the
# legacy "overloaded" text out of that path so a transient 529 doesn't
# get a 5-hour session wait.
_RATE_LIMIT_PATTERNS = [
    "hit your limit",
    "usage limit",
    "rate limit",
    "rate_limit",
    "overloaded",
]

# Matches the structured error raised by discovery._format_result_error()
# (e.g. "Claude API call failed: HTTP 529 | ..."). When this captures a
# status, we trust the code over textual patterns — 429 == rate limit,
# everything else is retriable.
_HTTP_STATUS_RE = re.compile(r"claude api call failed: http (\d+)", re.IGNORECASE)

MAX_CONSECUTIVE_ERRORS = 3


def _http_status_from_error(err_msg: str) -> int | None:
    """Pull the HTTP status off a discovery-formatted API error, if any."""
    m = _HTTP_STATUS_RE.search(err_msg)
    return int(m.group(1)) if m else None


def _is_rate_limit_error(err_msg: str) -> bool:
    """Check if error message indicates a rate limit."""
    status = _http_status_from_error(err_msg)
    if status is not None:
        # We surfaced the HTTP code ourselves — trust it. Only 429 is a
        # real rate limit; 5xx (incl. 529 overloaded) is transient and
        # belongs on the retry path even if the API body happens to
        # contain the word "overloaded".
        return status == 429
    lower = err_msg.lower()
    return any(p in lower for p in _RATE_LIMIT_PATTERNS)


def _is_retriable_error(err_msg: str) -> bool:
    """Check if error is likely retriable (e.g. exit code 1 from CLI)."""
    status = _http_status_from_error(err_msg)
    if status is not None:
        # 5xx is transient — retry. 429 is also "retriable" in the sense
        # that the engine should not break, but it's already routed via
        # _is_rate_limit_error() above before reaching this branch.
        return 500 <= status < 600 or status == 429
    lower = err_msg.lower()
    return (
        "exit code 1" in lower
        or "command failed" in lower
        or "connection" in lower
        or "timeout" in lower
        # claude-agent-sdk wraps a CLI result with is_error=true into
        # "Claude Code returned an error result: <subtype-or-errors>". The
        # CLI occasionally emits is_error=true with subtype="success" and an
        # empty errors[] mid-tool (e.g. a Bash invocation that gets cut off),
        # which is transient — a fresh session on retry recovers.
        or "returned an error result" in lower
        # discovery._run_query() raises this for ResultMessage.is_error=true
        # cases without an HTTP status (e.g. subtype="error_max_turns").
        # When a status *is* present, the HTTP branch above handles it.
        or "claude api call failed" in lower
    )


async def _wait_if_quiet_hours() -> None:
    """Sleep until quiet hours (22:00~03:00) are over."""
    import asyncio
    from datetime import datetime, timedelta

    now = datetime.now()
    hour = now.hour

    if hour >= QUIET_START or hour < QUIET_END:
        # Calculate wake time: next 03:00
        wake = now.replace(hour=QUIET_END, minute=0, second=0, microsecond=0)
        if hour >= QUIET_START:
            wake += timedelta(days=1)
        wait_secs = (wake - now).total_seconds()
        display.console.print(
            f"\n[dim]Quiet hours (22:00~03:00). "
            f"Sleeping until {wake.strftime('%H:%M')} "
            f"({wait_secs / 60:.0f} min)...[/dim]"
        )
        await asyncio.sleep(wait_secs)
        display.console.print("[dim]Quiet hours over, resuming...[/dim]")


async def _wait_until(when: object) -> None:
    """Sleep until the given local datetime (with a small buffer)."""
    import asyncio
    from datetime import datetime

    remaining = (when - datetime.now()).total_seconds()  # type: ignore[operator]
    if remaining > 0:
        display.console.print(
            f"[dim]Waiting until {when.strftime('%H:%M')} "  # type: ignore[attr-defined]
            f"({remaining / 60:.0f} min)...[/dim]"
        )
        await asyncio.sleep(remaining + 60)


async def _wait_for_next_session(throttle: SessionThrottle | None) -> None:
    """Wait until the current session ends, then return."""
    import asyncio
    from datetime import datetime

    session_end = throttle.session_end if throttle else None
    if session_end is not None:
        remaining = (session_end - datetime.now()).total_seconds()
        if remaining > 0:
            display.console.print(
                f"[dim]Session ends at {session_end.strftime('%H:%M')}. "
                f"Waiting {remaining / 60:.0f} min...[/dim]"
            )
            await asyncio.sleep(remaining + 60)  # +1min buffer
            return

    # No throttle, snapshot unavailable, or session already ended: wait 5 min.
    display.console.print("[dim]Waiting 5 min before retrying...[/dim]")
    await asyncio.sleep(300)


async def run_discovery_loop(
    plan_prompt: str,
    max_iterations: int | None = None,
    report_dir: Path | None = None,
    cwd: str | None = None,
    auto: bool = False,
    throttle: SessionThrottle | None = None,
    throttle_enabled: bool = False,
    resume: bool = True,
    stop_at: object | None = None,  # datetime.time
    model: str | None = None,
    max_turns: int = 80,
    backend: str = "claude",
    effort: str | None = None,
    retry_on_dry: int = 0,
) -> None:
    """Main discovery loop.

    When auto=False (interactive):
      find plan -> show -> user approves/rejects -> repeat

    When auto=True (unattended):
      find plan -> save to pending/ -> repeat

    When throttle is set, each iteration checks the time-proportional
    budget before querying Claude.

    When resume=True, subsequent iterations resume the same Claude session
    to preserve codebase analysis context between iterations.
    """
    import os

    discover_plan = _resolve_backend(backend)

    effective_cwd = cwd or os.getcwd()
    project_name = Path(effective_cwd).name

    if report_dir is None:
        report_dir = Path.home() / "claude-reports" / project_name

    state_mgr = StateManager(report_dir)
    state_mgr.load()

    from datetime import datetime as _dt

    iteration = 0
    session_approved = 0
    session_rejected = 0
    session_pending = 0
    session_id: str | None = None
    session_start_time = _dt.now()
    consecutive_errors = 0
    # Tracks how many consecutive iterations ended in found_nothing=true.
    # Reset whenever a real plan is discovered. Used by --retry-on-dry to
    # probe with a fresh session before giving up.
    consecutive_dry = 0

    try:
        while True:
            iteration += 1

            if max_iterations and iteration > max_iterations:
                display.console.print(
                    f"\n[yellow]Reached max iterations ({max_iterations}). Stopping.[/yellow]"
                )
                break

            # Stop at specified time
            if stop_at:
                from datetime import datetime
                now = datetime.now().time()
                if now >= stop_at:
                    display.console.print(
                        f"\n[yellow]Reached stop time ({stop_at.strftime('%H:%M')}). Stopping.[/yellow]"
                    )
                    break

            # Quiet hours: no queries 22:00~03:00
            await _wait_if_quiet_hours()

            # Refresh the OAuth utilization snapshot (cheap, in-memory cached;
            # only hits Anthropic when the cache TTL has elapsed). Then sleep
            # if either session or weekly utilization has reached target.
            if throttle_enabled and throttle:
                throttle.refresh()
                # Passing stop_at lets the throttle bail out of a long
                # weekly-reset wait (24h+) so the outer loop's --stop-at
                # check on the next iteration break out instead of parking
                # the daemon past the stop time.
                await throttle.wait_if_needed(stop_at=stop_at)
                # wait_if_needed yields to us when stop_at passes, but the
                # top-of-loop stop-at check has already run for THIS
                # iteration — falling through would call discover_plan()
                # past the stop time (observed 07-08: iter 1 kicked off at
                # 07:29, hit weekly hard cap, and only stopped after a
                # rate-limit round-trip). Re-check now so the run ends
                # cleanly at the stop time without any API calls.
                if stop_at:
                    from datetime import datetime
                    if datetime.now().time() >= stop_at:
                        display.console.print(
                            f"\n[yellow]Reached stop time "
                            f"({stop_at.strftime('%H:%M')}) after throttle "
                            f"wait. Stopping.[/yellow]"
                        )
                        break

            display.show_discovery_start(iteration)
            if throttle:
                display.console.print(f"  [dim]{throttle.status_line()}[/dim]")
            if session_id and resume:
                display.console.print(
                    f"  [dim]Resuming session {session_id[:8]}...[/dim]"
                )

            if session_id and resume:
                # Claude already knows the full list from the first iteration.
                # Only include plans discovered during this session.
                new_plans = [
                    r for r in state_mgr.state.rejected_plans
                    if r.rejected_at > session_start_time
                ]
                prompt = build_prompt(plan_prompt, new_plans)
            else:
                prompt = build_prompt(plan_prompt, state_mgr.state.rejected_plans)

            resume_id = session_id if resume else None

            try:
                with display.live_status() as status:

                    def on_activity(detail: str) -> None:
                        status.update(f"[dim]{rich_escape(detail)}[/dim]")

                    result = await discover_plan(
                        prompt=prompt,
                        cwd=effective_cwd,
                        resume_session_id=resume_id,
                        on_activity=on_activity,
                        model=model,
                        max_turns=max_turns,
                        effort=effort,
                    )
            except asyncio.TimeoutError:
                display.console.print(
                    "\n[yellow]Query timed out (45 min). Resetting session and retrying...[/yellow]"
                )
                session_id = None
                session_start_time = _dt.now()
                iteration -= 1
                continue
            except RateLimitError as e:
                display.console.print(
                    f"\n[yellow]Usage limit reached: {rich_escape(str(e)[:160])}[/yellow]"
                )
                if e.retry_at is not None:
                    await _wait_until(e.retry_at)
                else:
                    await _wait_for_next_session(throttle)
                session_id = None
                session_start_time = _dt.now()
                if throttle:
                    throttle.reinit()
                consecutive_errors = 0
                iteration -= 1
                continue
            except Exception as e:
                err_msg = str(e)
                if _is_rate_limit_error(err_msg):
                    display.console.print(
                        f"\n[yellow]Rate limit reached. Waiting for next session...[/yellow]"
                    )
                    await _wait_for_next_session(throttle)
                    session_id = None
                    session_start_time = _dt.now()
                    if throttle:
                        throttle.reinit()
                    consecutive_errors = 0
                    iteration -= 1
                    continue
                if "prompt is too long" in err_msg.lower() or "maximum buffer size" in err_msg.lower():
                    display.console.print(
                        f"\n[yellow]Session context too large. Resetting session and retrying...[/yellow]"
                    )
                    session_id = None
                    session_start_time = _dt.now()
                    iteration -= 1
                    continue
                # Retriable errors (e.g. exit code 1 from CLI = likely rate limit)
                if _is_retriable_error(err_msg):
                    consecutive_errors += 1
                    display.console.print(
                        f"\n[yellow]Error (attempt {consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): "
                        f"{err_msg[:120]}[/yellow]"
                    )
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        display.console.print(
                            f"\n[red]Too many consecutive errors. "
                            f"Treating as rate limit and waiting for next session...[/red]"
                        )
                        await _wait_for_next_session(throttle)
                        session_id = None
                        session_start_time = _dt.now()
                        if throttle:
                            throttle.reinit()
                        consecutive_errors = 0
                        iteration -= 1
                        continue
                    # Wait briefly and retry with fresh session
                    display.console.print(
                        "[dim]Resetting session and retrying in 30s...[/dim]"
                    )
                    await asyncio.sleep(30)
                    session_id = None
                    session_start_time = _dt.now()
                    iteration -= 1
                    continue
                # Unknown error: log and stop gracefully
                display.console.print(
                    f"\n[red]Unexpected error: {rich_escape(err_msg[:200])}[/red]"
                )
                display.console.print(
                    "[yellow]Stopping gracefully.[/yellow]"
                )
                break

            # Success: reset error counter
            consecutive_errors = 0

            # Capture session_id for next iteration
            if result.session_id:
                session_id = result.session_id

            cost_str = (
                f"Cost: ${result.cost_usd:.2f} | " if result.cost_usd else ""
            )
            display.console.print(
                f"  [dim]Turns: {result.num_turns} | "
                f"{cost_str}"
                f"Tokens: {result.total_tokens:,}[/dim]"
            )

            # Track usage for throttle
            if throttle:
                throttle.add_usage(result.cost_usd, result.total_tokens, result.model)

            if result.plan is None:
                display.console.print(
                    "\n[red]Failed to get structured output from Claude. Retrying...[/red]"
                )
                iteration -= 1
                continue

            if result.plan.found_nothing:
                if consecutive_dry < retry_on_dry:
                    consecutive_dry += 1
                    display.console.print(
                        f"\n[yellow]Model reported found_nothing. Probing again "
                        f"with a fresh session "
                        f"({consecutive_dry}/{retry_on_dry})...[/yellow]"
                    )
                    # Force a fresh session so the next iteration is not biased
                    # by the prior turn's "nothing left" framing. Counts toward
                    # --max like any other iteration.
                    session_id = None
                    session_start_time = _dt.now()
                    continue
                display.show_no_more_plans()
                break

            # A real plan came back — clear the dry-out streak so the next
            # found_nothing again gets the full retry budget.
            consecutive_dry = 0
            display.show_plan(result.plan, iteration)

            if auto:
                filepath = save_plan(
                    result.plan, iteration, report_dir, pending=True
                )
                state_mgr.add_pending(result.plan, markdown_path=str(filepath))
                session_pending += 1
                display.show_saved_pending(filepath)
            else:
                current_plan = result.plan
                while True:
                    action, feedback = display.ask_approval()

                    if action == "approve":
                        filepath = save_plan(current_plan, iteration, report_dir)
                        state_mgr.record_approval(
                            current_plan, markdown_path=str(filepath)
                        )
                        session_approved += 1
                        display.show_saved(filepath)
                        break
                    elif action == "reject":
                        state_mgr.add_rejection(current_plan, feedback)
                        session_rejected += 1
                        display.show_rejected(current_plan.title)
                        break
                    else:  # revise
                        display.console.print(
                            "[cyan]Sending feedback to Claude...[/cyan]"
                        )
                        revision_prompt = (
                            f"I have feedback on the plan you just proposed "
                            f"(\"{current_plan.title}\"):\n\n"
                            f"{feedback}\n\n"
                            f"Please revise the plan based on this feedback, "
                            f"or propose a completely different plan if the "
                            f"feedback invalidates the original idea."
                        )
                        try:
                            with display.live_status() as status:

                                def on_revise_activity(detail: str) -> None:
                                    status.update(f"[dim]{rich_escape(detail)}[/dim]")

                                revision = await discover_plan(
                                    prompt=revision_prompt,
                                    cwd=effective_cwd,
                                    resume_session_id=session_id,
                                    on_activity=on_revise_activity,
                                    model=model,
                                    max_turns=max_turns,
                                    effort=effort,
                                )
                        except Exception as e:
                            err_msg = str(e)
                            if _is_rate_limit_error(err_msg) or _is_retriable_error(err_msg):
                                display.console.print(
                                    f"\n[yellow]Error during revision: {rich_escape(err_msg[:120])}[/yellow]"
                                )
                                display.console.print(
                                    "[yellow]Waiting for next session...[/yellow]"
                                )
                                await _wait_for_next_session(throttle)
                                session_id = None
                                session_start_time = _dt.now()
                                if throttle:
                                    throttle.reinit()
                                break
                            display.console.print(
                                f"\n[red]Unexpected error during revision: {rich_escape(err_msg[:200])}[/red]"
                            )
                            break

                        if revision.session_id:
                            session_id = revision.session_id
                        if throttle:
                            throttle.add_usage(revision.cost_usd, revision.total_tokens, revision.model)

                        if revision.plan and not revision.plan.found_nothing:
                            current_plan = revision.plan
                            display.show_plan(current_plan, iteration)
                            # Loop back to ask y/n/r again
                        else:
                            display.console.print(
                                "\n[red]Revision failed to produce a plan.[/red]"
                            )
                            break

    except KeyboardInterrupt:
        display.console.print("\n[yellow]Interrupted by user.[/yellow]")

    display.show_summary(session_approved, session_rejected, session_pending)
