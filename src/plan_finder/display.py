from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.markup import escape as rich_escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.spinner import Spinner
from rich.table import Table

from .models import DiscoveredPlan, RejectionRecord

console = Console()


def show_plan(plan: DiscoveredPlan, iteration: int) -> None:
    """Display a discovered plan in a rich panel."""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="bold cyan")
    table.add_column("Value")
    table.add_row("Category", plan.category.value)
    table.add_row("Priority", f"{plan.priority}/5")
    table.add_row("Effort", plan.estimated_effort.value)
    files_str = ", ".join(rich_escape(f) for f in plan.files_affected[:5])
    if len(plan.files_affected) > 5:
        files_str += "..."
    table.add_row("Files", files_str)

    console.print()
    console.print(
        Panel(
            f"[bold]{rich_escape(plan.title)}[/bold]\n\n"
            f"{rich_escape(plan.description)}\n\n"
            f"[dim]Rationale:[/dim] {rich_escape(plan.rationale)}",
            title=f"[green]Plan #{iteration}[/green]",
            border_style="green",
        )
    )
    console.print(table)

    if plan.implementation_steps:
        console.print("\n[bold]Implementation Steps:[/bold]")
        for i, step in enumerate(plan.implementation_steps, 1):
            console.print(f"  {i}. {rich_escape(step)}")

    if plan.risks:
        console.print("\n[bold yellow]Risks:[/bold yellow]")
        for risk in plan.risks:
            console.print(f"  - {rich_escape(risk)}")


def _flush_stdin() -> None:
    """Discard any buffered input (e.g. Enter pressed during spinner)."""
    import sys
    import termios

    try:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (termios.error, OSError, ValueError):
        pass


def _char_width(ch: str) -> int:
    """Return display width of a character (2 for CJK wide, 1 otherwise)."""
    import unicodedata

    eaw = unicodedata.east_asian_width(ch)
    return 2 if eaw in ("W", "F") else 1


def _raw_input(prompt: str = "") -> str:
    """Read a line in cbreak mode with correct CJK wide-char backspace.

    Neither macOS libedit (Python readline) nor the kernel's cooked-mode
    line discipline understand that CJK characters occupy 2 terminal
    columns.  Backspace only rewinds 1 column, leaving ghost characters.

    This function switches to cbreak mode (character-at-a-time, no echo)
    and manages cursor movement manually using each character's actual
    display width so that backspace over Korean/CJK chars works correctly.
    """
    import sys
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    if prompt:
        sys.stdout.write(prompt)
        sys.stdout.flush()

    buf: list[str] = []
    try:
        tty.setcbreak(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\n", "\r"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                break
            elif ch in ("\x7f", "\x08"):  # backspace / delete
                if buf:
                    removed = buf.pop()
                    w = _char_width(removed)
                    sys.stdout.write("\b" * w + " " * w + "\b" * w)
                    sys.stdout.flush()
            elif ch == "\x03":  # Ctrl-C
                sys.stdout.write("\n")
                sys.stdout.flush()
                raise KeyboardInterrupt
            elif ch == "\x15":  # Ctrl-U: kill line
                total_w = sum(_char_width(c) for c in buf)
                sys.stdout.write("\b" * total_w + " " * total_w + "\b" * total_w)
                sys.stdout.flush()
                buf.clear()
            elif ch == "\x1b":  # escape sequence — consume and ignore
                next1 = sys.stdin.read(1)
                if next1 == "[":
                    sys.stdin.read(1)
            elif ord(ch) >= 32:  # printable
                buf.append(ch)
                sys.stdout.write(ch)
                sys.stdout.flush()
        return "".join(buf)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def ask_approval() -> tuple[str, str]:
    """Ask user to approve, reject, or request revision.

    Returns (action, feedback) where:
      action: "approve" | "reject" | "revise"
      feedback: rejection reason or revision feedback
    """
    _flush_stdin()
    console.print()

    while True:
        console.print(
            "[bold]Action[/bold] (y=approve, n=reject, r=revise)"
        )
        raw = _raw_input("[y/n/r] (y): ").strip().lower()
        if raw == "":
            choice = "y"
        elif raw in ("y", "n", "r"):
            choice = raw
        else:
            console.print(f"[red]Please enter y, n, or r (got '{raw}')[/red]")
            continue
        break

    if choice == "y":
        return "approve", ""
    if choice == "r":
        console.print("[bold]Revision feedback[/bold]")
        feedback = _raw_input(": ")
        return "revise", feedback
    # reject
    console.print("[dim]Rejection reason (optional, press Enter to skip)[/dim]")
    reason = _raw_input("(): ")
    return "reject", reason


def show_saved(filepath: Path) -> None:
    console.print(f"\n[bold green]Plan saved to:[/bold green] {filepath}")


def show_saved_pending(filepath: Path) -> None:
    console.print(f"\n[bold cyan]Pending plan saved to:[/bold cyan] {filepath}")


def show_rejected(title: str) -> None:
    console.print(
        f"\n[bold red]Rejected:[/bold red] {rich_escape(title)} "
        "(will be skipped in future iterations)"
    )


def show_rejected_list(rejected_plans: list[RejectionRecord]) -> None:
    """Show a summary of previously rejected plans."""
    if not rejected_plans:
        return

    from .prompts import MAX_REJECTIONS_IN_PROMPT

    total = len(rejected_plans)
    used = min(total, MAX_REJECTIONS_IN_PROMPT)
    console.print(
        f"\n[bold yellow]Previously rejected plans ({total}):[/bold yellow]"
    )
    if total > MAX_REJECTIONS_IN_PROMPT:
        console.print(
            f"  [dim]({total - used} older plans omitted, "
            f"most recent {used} will be sent to Claude)[/dim]"
        )
    shown = rejected_plans[-MAX_REJECTIONS_IN_PROMPT:]
    offset = max(0, total - MAX_REJECTIONS_IN_PROMPT)
    for i, r in enumerate(shown, offset + 1):
        reason_str = f" — {rich_escape(r.reason)}" if r.reason else ""
        console.print(
            f"  [dim]{i}.[/dim] [{rich_escape(r.category)}] "
            f"{rich_escape(r.title)}{reason_str}"
        )


def show_discovery_start(iteration: int) -> None:
    console.print(f"\n[bold blue]--- Iteration {iteration} ---[/bold blue]")


def show_no_more_plans() -> None:
    console.print(
        "\n[bold green]No more improvements found. The codebase looks good![/bold green]"
    )


def show_summary(approved: int, rejected: int, pending: int = 0) -> None:
    console.print("\n[bold]Session Summary:[/bold]")
    console.print(f"  Approved: {approved}")
    console.print(f"  Rejected: {rejected}")
    if pending:
        console.print(f"  Pending review: {pending}")


def show_pending_plan(filepath: Path, index: int, total: int) -> None:
    """Display a pending plan markdown file using rich Markdown rendering."""
    from rich.markdown import Markdown

    content = filepath.read_text(encoding="utf-8")
    console.print()
    console.print(
        Panel(
            Markdown(content),
            title=f"[cyan]Pending Plan [{index}/{total}][/cyan]",
            border_style="cyan",
        )
    )


def ask_review_action() -> tuple[str, str]:
    """Ask user to approve, reject, or skip a pending plan.

    Returns (action, reason) where:
      action: "approve" | "reject" | "skip"
      reason: rejection reason (only for reject)
    """
    _flush_stdin()
    console.print()
    choice = Prompt.ask(
        "[bold]Action[/bold] [dim](y=approve, n=reject, s=skip)[/dim]",
        choices=["y", "n", "s"],
        default="s",
    )
    if choice == "y":
        return "approve", ""
    if choice == "n":
        reason = Prompt.ask(
            "[dim]Rejection reason (optional, press Enter to skip)[/dim]",
            default="",
        )
        return "reject", reason
    return "skip", ""


class LiveStatus:
    """Spinner + updatable status text."""

    def __init__(self) -> None:
        self._text = "Claude is analyzing the codebase..."
        self._live: Live | None = None

    def _render(self) -> Spinner:
        return Spinner("dots", text=self._text)

    def update(self, text: str) -> None:
        self._text = text
        if self._live:
            try:
                self._live.update(self._render())
            except Exception:
                pass  # swallow markup errors in status display

    def __enter__(self) -> LiveStatus:
        self._live = Live(self._render(), console=console, refresh_per_second=8)
        self._live.__enter__()
        return self

    def __exit__(self, *args: object) -> None:
        if self._live:
            self._live.__exit__(*args)


def live_status() -> LiveStatus:
    """Create a live status display for discovery phase."""
    return LiveStatus()
