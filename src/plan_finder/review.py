from __future__ import annotations

import re
import shutil
from pathlib import Path

from .display import (
    ask_review_action,
    console,
    show_pending_plan,
    show_summary,
)
from .state import StateManager


def _parse_title(filepath: Path) -> str:
    """Extract title from first '# ...' line in markdown file."""
    for line in filepath.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    # Fallback to filename
    return filepath.stem.replace("-", " ").replace("_", " ")


def _update_status_in_markdown(filepath: Path, new_status: str) -> None:
    """Update 'Status: ...' line in markdown content if present."""
    content = filepath.read_text(encoding="utf-8")
    updated = re.sub(
        r"(?m)^(\*\*Status\*\*:\s*).*$",
        rf"\g<1>{new_status}",
        content,
    )
    if updated == content:
        # Also try plain "Status:" format
        updated = re.sub(
            r"(?m)^(Status:\s*).*$",
            rf"\g<1>{new_status}",
            content,
        )
    if updated == content:
        updated = content.replace(
            "*Pending review (auto mode)*",
            "*Approved and saved by plan-finder*",
            1,
        )
    filepath.write_text(updated, encoding="utf-8")


def run_review(report_dir: Path) -> None:
    """Scan pending/ dir, show each plan, ask approve/reject/skip."""
    pending_dir = report_dir / "pending"

    if not pending_dir.exists():
        console.print("[yellow]No pending directory found.[/yellow]")
        return

    md_files = sorted(pending_dir.glob("*.md"), key=lambda p: p.stat().st_mtime)

    if not md_files:
        console.print("[yellow]No pending plans to review.[/yellow]")
        return

    console.print(
        f"\n[bold]Found {len(md_files)} pending plan(s) to review.[/bold]"
    )

    mgr = StateManager(report_dir)
    mgr.load()

    approved = 0
    rejected = 0
    skipped = 0

    for i, filepath in enumerate(md_files, 1):
        title = _parse_title(filepath)

        show_pending_plan(filepath, i, len(md_files))
        action, reason = ask_review_action()

        if action == "approve":
            # Move file to report_dir (parent of pending/)
            dest = report_dir / filepath.name
            if not mgr.approve_pending(title):
                console.print(
                    f"[yellow]Warning:[/yellow] Could not update pending state for "
                    f"{title}. Leaving file unchanged."
                )
                skipped += 1
                continue
            _update_status_in_markdown(filepath, "Approved")
            shutil.move(str(filepath), str(dest))
            console.print(
                f"[bold green]Approved:[/bold green] {title}\n"
                f"  Moved to {dest}"
            )
            approved += 1

        elif action == "reject":
            if not mgr.reject_pending(title, reason):
                console.print(
                    f"[yellow]Warning:[/yellow] Could not update pending state for "
                    f"{title}. Leaving file unchanged."
                )
                skipped += 1
                continue
            filepath.unlink()
            reason_str = f" — {reason}" if reason else ""
            console.print(
                f"[bold red]Rejected:[/bold red] {title}{reason_str}"
            )
            rejected += 1

        else:
            console.print(f"[dim]Skipped:[/dim] {title}")
            skipped += 1

    console.print()
    show_summary(approved, rejected, skipped)
