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
from invoice_agent.ingestion.loader import invoice_files, is_invoice
from invoice_agent.models import RunStatus
from invoice_agent.orchestrator import ReviewError, RunState, load_state, resolve_review, run_pipeline

console = Console()


def closest(name: str, options: list[Path]) -> Path | None:
    match = difflib.get_close_matches(name, [p.name for p in options], n=1, cutoff=0.6)
    return next(p for p in options if p.name == match[0]) if match else None


def suggest_path(path: Path) -> str | None:
    """Fix a mistyped path one part at a time (folders, then the name); otherwise find the name under data/."""
    folder = Path(path.anchor) if path.is_absolute() else Path(".")
    *folders, name = path.parts[1:] if path.is_absolute() else path.parts
    for part in folders:
        fixed = folder / part if (folder / part).is_dir() else closest(part, [p for p in folder.iterdir() if p.is_dir()])
        if fixed is None:
            break
        folder = fixed
    else:
        options = [
            p for p in folder.iterdir()
            if is_invoice(p) or (not path.suffix and p.is_dir() and any(map(is_invoice, p.iterdir())))
        ]  # fmt: skip
        if match := closest(name, options):
            return str(match)
    match = closest(name, [p for p in (config.ROOT / "data").rglob("*") if is_invoice(p)])
    if match is None:
        return None
    return str(match.relative_to(Path.cwd())) if match.is_relative_to(Path.cwd()) else str(match)


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
    render_result(console, resolved, verbose=reporter.verbose)
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


def serve(port: int) -> int:
    import uvicorn

    from api.app import WEB_DIST, app

    configure_logging()
    if not WEB_DIST.is_dir():
        console.print("[yellow]web/dist is missing, so only the API is served.[/yellow] Build it: cd web && npm run build")
    console.print(f"Dashboard: [bold]http://localhost:{port}[/bold]  (Ctrl-C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Process Acme Corp invoices end to end.")
    parser.add_argument("command", nargs="?", choices=["serve"], help="serve: open the dashboard at localhost")
    parser.add_argument("--invoice_path", help="invoice file, or a folder to process in filename order")
    parser.add_argument("-v", "--verbose", action="store_true", help="show LLM and tool calls")
    parser.add_argument("--reset", action="store_true", help="delete and re-create the database")
    parser.add_argument("--port", type=int, default=8000, help="dashboard port (with serve)")
    args = parser.parse_args(argv)

    if not config.xai_api_key():
        console.print(
            "[red]XAI_API_KEY is not set.[/red] Copy .env.example to .env and add your xAI key:\n"
            "  cp .env.example .env"
        )
        return 1
    if args.command is None and args.invoice_path is None and not args.reset:
        parser.error("--invoice_path is required")

    db.init_db(reset=args.reset)
    if args.reset:
        console.print("[dim]Database reset.[/dim]")
    if args.command == "serve":
        return serve(args.port)
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
        render_result(console, state, verbose=args.verbose)
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
        render_result(console, state, verbose=args.verbose)
        states.append(state)
    render_summary(console, states)
    if interactive:
        for state in states:
            if state.status == RunStatus.NEEDS_REVIEW:
                review(state, reporter)
    return 1 if any(s.status == RunStatus.FAILED for s in states) else 0


if __name__ == "__main__":
    sys.exit(main())
