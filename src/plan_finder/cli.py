from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer
from typing_extensions import Annotated

app = typer.Typer(
    name="plan-finder",
    help="Iteratively discover improvement plans in a codebase using Claude AI.",
    no_args_is_help=False,
)


@app.command()
def main(
    prompt: Annotated[
        Optional[str],
        typer.Option(
            "--prompt",
            "-p",
            help="Plan prompt. If omitted, you'll be asked interactively.",
        ),
    ] = None,
    max_iterations: Annotated[
        Optional[int],
        typer.Option(
            "--max",
            "-m",
            help="Maximum number of discovery iterations.",
        ),
    ] = None,
    report_dir: Annotated[
        Optional[Path],
        typer.Option(
            "--report-dir",
            "-d",
            help="Directory to save approved plans. Default: ~/claude-reports/{project}",
        ),
    ] = None,
    auto: Annotated[
        bool,
        typer.Option(
            "--auto",
            help="Auto mode: find plans unattended and save to pending/. Requires --prompt.",
        ),
    ] = False,
    session_budget: Annotated[
        float,
        typer.Option(
            "--session-budget",
            help="Session budget in USD. Default $40.",
        ),
    ] = 40.0,
    no_resume: Annotated[
        bool,
        typer.Option(
            "--no-resume",
            help="Don't resume previous Claude session between iterations. Each iteration starts fresh.",
        ),
    ] = False,
    stop_at: Annotated[
        Optional[str],
        typer.Option(
            "--stop-at",
            help="Stop after this time (HH:MM). e.g. --stop-at 07:30",
        ),
    ] = None,
    no_throttle: Annotated[
        bool,
        typer.Option(
            "--no-throttle",
            help="Disable cost-based throttling (enabled by default).",
        ),
    ] = False,
    model: Annotated[
        Optional[str],
        typer.Option(
            "--model",
            help="Claude model to use (e.g. claude-opus-4-6, claude-sonnet-4-5-20250929).",
        ),
    ] = None,
    review: Annotated[
        bool,
        typer.Option(
            "--review",
            help="Review pending plans interactively.",
        ),
    ] = False,
    clear_rejections: Annotated[
        bool,
        typer.Option(
            "--clear-rejections",
            help="Clear previously rejected plans before starting.",
        ),
    ] = False,
) -> None:
    """Discover improvement plans in the current codebase.

    Runs Claude AI in a loop to analyze the cwd codebase. Each discovered
    plan is presented for approval. Approved plans are saved as markdown
    files. Rejected plans are remembered and skipped in future runs.
    """
    import os

    from rich.prompt import Prompt

    from .display import console, show_rejected_list
    from .state import StateManager

    cwd = os.getcwd()
    project_name = Path(cwd).name

    effective_report_dir = report_dir or (
        Path.home() / "claude-reports" / project_name
    )

    if clear_rejections:
        mgr = StateManager(effective_report_dir)
        mgr.load()
        mgr.clear_rejections()
        console.print("[green]Rejection history cleared.[/green]")

    if review:
        from .review import run_review

        run_review(effective_report_dir)
        raise typer.Exit(0)

    # Auto mode requires --prompt
    if auto and not prompt:
        console.print("[red]--auto requires --prompt. Exiting.[/red]")
        raise typer.Exit(1)

    # Show existing rejections if any
    mgr = StateManager(effective_report_dir)
    mgr.load()
    show_rejected_list(mgr.state.rejected_plans)

    # Prompt required: ask interactively if not provided via --prompt
    if prompt is None:
        console.print()
        prompt = Prompt.ask(
            "[bold]Enter your plan prompt[/bold]\n"
            "[dim](e.g. 'Find any improvement and propose a plan')[/dim]"
        )
        if not prompt.strip():
            console.print("[red]Prompt is required. Exiting.[/red]")
            raise typer.Exit(1)

    if auto:
        console.print(
            "\n[bold cyan]Running in auto mode.[/bold cyan] "
            f"Plans will be saved to [bold]{effective_report_dir / 'pending'}[/bold]"
        )

    # Session info always shown; throttle waiting when auto or --throttle
    from .throttle import SessionThrottle

    session_throttle = SessionThrottle(
        session_budget=session_budget,
    )

    from .engine import run_discovery_loop

    # Parse stop_at time
    stop_at_time = None
    if stop_at:
        try:
            h, m = stop_at.split(":")
            from datetime import time as dt_time
            stop_at_time = dt_time(int(h), int(m))
        except ValueError:
            console.print(f"[red]Invalid --stop-at format: {stop_at}. Use HH:MM.[/red]")
            raise typer.Exit(1)

    asyncio.run(
        run_discovery_loop(
            plan_prompt=prompt,
            max_iterations=max_iterations,
            report_dir=effective_report_dir,
            cwd=cwd,
            auto=auto,
            throttle=session_throttle,
            throttle_enabled=not no_throttle,
            resume=not no_resume,
            stop_at=stop_at_time,
            model=model,
        )
    )


if __name__ == "__main__":
    app()
