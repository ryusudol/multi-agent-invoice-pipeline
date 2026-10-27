"""Rich rendering for the CLI: live stage checklist, findings table, decision card, batch summary."""

import re

from rich.console import Console, Group
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.status import Status
from rich.table import Table
from rich.text import Text

from invoice_agent.events import EventType, PipelineEvent
from invoice_agent.ingestion.normalize import format_money
from invoice_agent.models import Outcome, RunStatus, Severity
from invoice_agent.orchestrator import RunState

STATUS_STYLE = {
    RunStatus.PAID: ("PAID", "bold green"),
    RunStatus.REJECTED: ("REJECTED", "bold red"),
    RunStatus.NEEDS_REVIEW: ("NEEDS REVIEW", "bold yellow"),
    RunStatus.DUPLICATE: ("DUPLICATE", "bold blue"),
    RunStatus.FAILED: ("FAILED", "bold magenta"),
    RunStatus.PROCESSING: ("PROCESSING", "bold"),
}

STAGE_LABEL = {
    "load": "Load",
    "extract": "Extract",
    "check_duplicate": "Duplicate check",
    "validate": "Validate",
    "policy_gate": "Policy gate",
    "approval": "Approval",
    "payment": "Payment",
    "rejection": "Rejection",
}

STAGE_ACTIVE = {
    "load": "Loading",
    "extract": "Extracting",
    "check_duplicate": "Checking for duplicates",
    "validate": "Validating",
    "policy_gate": "Checking policy",
    "approval": "Reviewing for approval",
    "payment": "Paying",
    "rejection": "Rejecting",
}


class LiveReporter:
    """Event callback: a spinner for the running stage and one line per notable event."""

    def __init__(self, console: Console, verbose: bool = False) -> None:
        self.console = console
        self.verbose = verbose
        self.status: Status | None = None

    def __call__(self, event: PipelineEvent) -> None:
        label = STAGE_LABEL.get(event.stage, event.stage)
        match event.type:
            case EventType.RUN_STARTED:
                self.console.print(f"[bold]{event.message}[/bold]")
            case EventType.STAGE_STARTED:
                self._spin(f"{STAGE_ACTIVE.get(event.stage, label)}…")
            case EventType.STAGE_COMPLETED:
                self._print(f"  [green]✓[/green] [bold]{label:<16}[/bold] {_escape(event.message)}")
            case EventType.EXTRACTION_RETRY:
                self._print(f"  [yellow]↻[/yellow] {_escape(event.message)}")
            case EventType.CRITIQUE:
                mark = "[green]⇄[/green]" if event.payload.get("agrees") else "[yellow]⇄[/yellow]"
                self._print(f"  {mark} {_escape(event.message)}")
                if self.verbose:
                    for issue in event.payload.get("issues", []):
                        self._print(f"      [dim]- {_escape(issue)}[/dim]")
            case EventType.MATCH_OVERRIDE | EventType.POLICY_OVERRIDE:
                self._print(f"  [yellow]![/yellow] {_escape(event.message)}")
            case EventType.ERROR:
                self._print(f"  [red]✗ {label}: {_escape(event.message)}[/red]")
            case EventType.REVIEW_RESOLVED:
                self._print(f"  [cyan]✎[/cyan] {_escape(event.message)}")
            case EventType.TOOL_CALL if self.verbose:
                self._print(f"      [dim]tool {_escape(event.message)}[/dim]")
            case EventType.AGENT_OUTPUT if self.verbose:
                usage = event.payload.get("usage", {})
                self._print(
                    f"      [dim]{event.payload.get('agent')} agent: {usage.get('requests')} request(s), "
                    f"{usage.get('input_tokens')} in / {usage.get('output_tokens')} out tokens[/dim]"
                )
            case EventType.RUN_FINISHED:
                self._stop()

    def _spin(self, text: str) -> None:
        if self.status is None:
            self.status = self.console.status(text)
            self.status.start()
        else:
            self.status.update(text)

    def _print(self, text: str) -> None:
        self.console.print(text, highlight=False)

    def _stop(self) -> None:
        if self.status is not None:
            self.status.stop()
            self.status = None


def _escape(text: str) -> str:
    return text.replace("[", r"\[")


# A sentence ends at . ! or ? followed by whitespace and a capital, digit, or $; "$7,185.00" never splits.
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9$])")


def bullets(text: str) -> Table | str:
    """One sentence per line with a hanging indent, so a long rationale scans as a list."""
    sentences = [s for s in SENTENCE_END.split(text.strip()) if s]
    if len(sentences) < 2:
        return text
    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="dim")
    grid.add_column()
    for sentence in sentences:
        grid.add_row("•", sentence)
    return grid


def status_text(status: RunStatus) -> Text:
    label, style = STATUS_STYLE[status]
    return Text(label, style=style)


def render_result(console: Console, state: RunState) -> None:
    invoice = state.invoice
    title = Text.assemble(
        status_text(state.status),
        "  ",
        (invoice.invoice_number or "no number") if invoice else state.name,
        " · ",
        (invoice.vendor or "no vendor") if invoice else "",
    )
    body: list = []
    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="dim")
    summary.add_column()
    summary.add_row("File", state.name)
    summary.add_row("Total", format_money(state.total_cents, state.currency))
    if state.policy:
        summary.add_row("Policy", f"{state.policy.mode}")
    if state.decision:
        d = state.decision
        summary.add_row("Decided by", f"{d.decided_by.replace('_', ' + ')}" + (f" after {d.rounds} critique round(s)" if d.rounds else ""))
        summary.add_row("Rationale", bullets(d.rationale))
        if d.review:
            summary.add_row("Reviewer", f"{d.review.reviewer}: {d.review.outcome}" + (f" — {d.review.comment}" if d.review.comment else ""))
        if d.critic_issues:
            summary.add_row("Critic issues", "\n".join(f"• {i}" for i in d.critic_issues))
    if state.payment:
        p = state.payment
        summary.add_row("Payment", f"{p.status.replace('_', ' ')}: {format_money(p.amount_cents, p.currency)}  [dim]{p.idempotency_key}[/dim]")
    if state.status == RunStatus.DUPLICATE and state.duplicate:
        summary.add_row("Duplicate of", f"run #{state.duplicate.earlier_run_id} ({state.duplicate.earlier_status})")
        summary.add_row("", "[dim]Run with --reset to start from an empty database.[/dim]")
    if state.error:
        summary.add_row("Error", Text(state.error, style="magenta"))
    usage = state.usage
    summary.add_row("LLM usage", f"{usage.requests} requests · {usage.input_tokens:,} in / {usage.output_tokens:,} out tokens · {state.duration_ms / 1000:.1f}s")
    body.append(summary)

    if state.validation and state.validation.findings:
        findings = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        findings.add_column("", width=1)
        findings.add_column("Finding")
        findings.add_column("Detail")
        for f in state.validation.findings:
            mark = Text("✗", style="red") if f.severity == Severity.ERROR else Text("!", style="yellow")
            findings.add_row(mark, f.code.value.replace("_", " "), f.message)
        body += [Text(""), findings]

    _, style = STATUS_STYLE[state.status]
    console.print(Panel(Group(*body), title=title, title_align="left", border_style=style.split()[-1]))


def render_summary(console: Console, states: list[RunState]) -> None:
    table = Table(title="Batch summary", header_style="bold")
    for column in ("File", "Invoice", "Vendor", "Total", "Status", "Reason"):
        table.add_column(column, overflow="fold")
    for state in states:
        inv = state.invoice
        if state.status == RunStatus.DUPLICATE and state.duplicate:
            reason = f"same as run #{state.duplicate.earlier_run_id} ({state.duplicate.earlier_status})"
        elif state.error:
            reason = state.error
        elif state.payment:
            reason = f"paid {format_money(state.payment.amount_cents, state.payment.currency)}"
        elif state.decision:
            reason = "; ".join(state.decision.reasons[:2])
        else:
            reason = ""
        table.add_row(
            state.name,
            inv.invoice_number if inv and inv.invoice_number else "—",
            inv.vendor if inv and inv.vendor else "—",
            format_money(state.total_cents, state.currency),
            status_text(state.status),
            reason,
        )
    console.print(table)
    counts: dict[RunStatus, int] = {}
    for state in states:
        counts[state.status] = counts.get(state.status, 0) + 1
    paid = sum(s.payment.amount_cents for s in states if s.payment and s.payment.currency == "USD")
    parts = [f"{n} {STATUS_STYLE[status][0].lower()}" for status, n in counts.items()]
    console.print(f"{len(states)} invoice(s): " + ", ".join(parts) + f" · {format_money(paid)} paid")


def ask_review(console: Console, state: RunState) -> tuple[Outcome, str] | None:
    """Ask a human to approve or reject. None means skip for now."""
    invoice = state.invoice
    name = f"{invoice.invoice_number} · {invoice.vendor}" if invoice else state.name
    console.print(f"\n[bold yellow]Review needed:[/bold yellow] {name} — {format_money(state.total_cents, state.currency)}")
    for reason in state.decision.reasons if state.decision else []:
        console.print(f"  • {reason}")
    choice = Prompt.ask("Decision", choices=["approve", "reject", "skip"], default="skip", console=console)
    if choice == "skip":
        return None
    comment = Prompt.ask("Comment", default="", console=console)
    if choice == "approve" and not Confirm.ask(f"Pay {format_money(state.total_cents, state.currency)}?", console=console):
        return None
    return Outcome(choice), comment
