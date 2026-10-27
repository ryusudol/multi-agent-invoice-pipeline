"""Intake agent: raw text of any format -> Invoice, with self-correction against the source."""

from pydantic_ai import Agent, ModelRetry, RunContext

from invoice_agent import config
from invoice_agent.agents.common import MODEL, AgentDeps, run_agent
from invoice_agent.events import EventType
from invoice_agent.ingestion.check import check_extraction, retry_message
from invoice_agent.models import Invoice, Usage

INSTRUCTIONS = """\
You extract invoice data for Acme Corp's accounts-payable team. The document may be plain text, an email,
JSON, CSV, XML, or text pulled from a PDF. Expect typos, OCR errors (the letter O written for a zero),
odd labels, and missing data.

Rules:
- Copy what the document states. Never compute, infer, or fill in a value that is not written.
  If a field is absent or blank, return null. A blank vendor is null, not a guess.
- Fix only OCR character errors in numbers ('2O26' is 2026, '$3,500.O0' is 3500.00).
- Each invoice line is its own line item, in order, even when the same item repeats. Keep the item
  name as written, including a parenthetical such as '(rush order)'. Put per-line notes in `note`.
- Only fill stated_tax_rate when a rate (like '8%' or 0.08) is written. A tax line with no rate has a null rate.
- If the document gives a single amount (e.g. 'Amt'), that is stated_total.
- risk_signals: list what a careful AP clerk would question as potentially fraudulent: pressure to pay
  immediately, unusual payment instructions, a due date that is not a real date or is before the invoice
  date, suspicious sender details, and similar. Quote the exact text. Do not report arithmetic or stock problems; other checks handle those. Leave the list empty
  when nothing is suspicious.
"""

intake_agent = Agent(
    MODEL,
    name="intake",
    deps_type=AgentDeps,
    output_type=Invoice,
    instructions=INSTRUCTIONS,
    retries={"output": config.EXTRACTION_RETRIES},
    defer_model_check=True,
)


@intake_agent.output_validator
def check_against_source(ctx: RunContext[AgentDeps], invoice: Invoice) -> Invoice:
    """The check_extraction step: schema is enforced by Pydantic; values must appear in the source."""
    problems = check_extraction(invoice, ctx.deps.source_text)
    if problems:
        attempt = ctx.retry + 1
        ctx.deps.sink.emit(
            ctx.deps.stage,
            EventType.EXTRACTION_RETRY,
            f"Extraction check failed (attempt {attempt}): " + "; ".join(problems),
            attempt=attempt,
            problems=problems,
            will_retry=attempt <= config.EXTRACTION_RETRIES,
        )
        raise ModelRetry(retry_message(problems))
    return invoice


def extract(source_name: str, source_text: str, deps: AgentDeps, usage: Usage) -> Invoice:
    prompt = f"Source file: {source_name}\n\n<document>\n{source_text}\n</document>"
    return run_agent(intake_agent, prompt, deps, usage)
