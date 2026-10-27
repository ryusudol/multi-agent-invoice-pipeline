"""CLI entry point: python main.py --invoice_path=data/invoices/invoice_1001.txt"""

import argparse
import difflib
import getpass
import sys
from pathlib import Path

from rich.console import Console

from invoice_agent import config, db
from invoice_agent.console import LiveReporter, ask_review, render_result, render_summary
from invoice_agent.events import configure_logging
from invoice_agent.models import RunStatus
from invoice_agent.orchestrator import ReviewError, RunState, load_state, resolve_review, run_pipeline

console = Console()


def suggest_path(path: Path) -> str | None:
    folder = path.parent if path.parent.is_dir() else Path(".")
    candidates = [p.name for p in folder.iterdir()] if folder.is_dir() else []
    match = difflib.get_close_matches(path.name, candidates, n=1, cutoff=0.5)
    return str(folder / match[0]) if match else None


def invoice_files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in config.SUPPORTED_SUFFIXES)


def review(state: RunState, reporter: LiveReporter) -> RunState:
    answer = ask_review(console, state)
    if answer is None:
        console.print("[dim]Left at needs_review.[/dim]")
        return state
    outcome, comment = answer
    try:
        resolved = resolve_review(state.run_id, outcome, comment, reviewer=getpass.getuser(), on_event=reporter)
    except ReviewError as exc:
        console.print(f"[red]{exc}[/red]")
        return state
    render_result(console, resolved)
    return resolved


def pending_review(state: RunState) -> RunState | None:
    """The run that is waiting for a human: this one, or the earlier run this duplicate points at."""
    if state.status == RunStatus.NEEDS_REVIEW:
        return state
    if state.status == RunStatus.DUPLICATE and state.duplicate and state.duplicate.earlier_status == RunStatus.NEEDS_REVIEW:
        assert state.duplicate.earlier_run_id is not None
        earlier = load_state(state.duplicate.earlier_run_id)
        return earlier if earlier.status == RunStatus.NEEDS_REVIEW else None
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Process Acme Corp invoices end to end.")
    parser.add_argument("--invoice_path", help="invoice file, or a folder to process in filename order")
    parser.add_argument("-v", "--verbose", action="store_true", help="show LLM and tool calls")
    parser.add_argument("--reset", action="store_true", help="delete and re-create the database")
    args = parser.parse_args(argv)

    if not config.xai_api_key():
        console.print(
            "[red]XAI_API_KEY is not set.[/red] Copy .env.example to .env and add your xAI key:\n"
            "  cp .env.example .env"
        )
        return 1
    if args.invoice_path is None and not args.reset:
        parser.error("--invoice_path is required")

    db.init_db(reset=args.reset)
    if args.reset:
        console.print("[dim]Database reset.[/dim]")
    if args.invoice_path is None:
        return 0

    path = Path(args.invoice_path)
    if not path.exists():
        hint = suggest_path(path)
        console.print(f"[red]{path} does not exist.[/red]" + (f" Did you mean {hint}?" if hint else ""))
        return 2

    configure_logging()
    reporter = LiveReporter(console, verbose=args.verbose)
    interactive = sys.stdin.isatty()

    if path.is_file():
        state = run_pipeline(path, on_event=reporter)
        render_result(console, state)
        waiting = pending_review(state)
        if waiting and interactive:
            review(waiting, reporter)
        return 1 if state.status == RunStatus.FAILED else 0

    files = invoice_files(path)
    if not files:
        console.print(f"[red]No invoice files in {path}[/red] (expected {', '.join(config.SUPPORTED_SUFFIXES)})")
        return 2
    states = []
    for file in files:
        state = run_pipeline(file, on_event=reporter)
        render_result(console, state)
        states.append(state)
    render_summary(console, states)
    if interactive:
        for state in states:
            if state.status == RunStatus.NEEDS_REVIEW:
                review(state, reporter)
    return 1 if any(s.status == RunStatus.FAILED for s in states) else 0


if __name__ == "__main__":
    sys.exit(main())
