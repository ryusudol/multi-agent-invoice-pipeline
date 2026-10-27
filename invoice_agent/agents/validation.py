"""Validation agent: matches invoice lines to the catalog with tools. Code then verifies every match."""

from pydantic_ai import Agent

from invoice_agent.agents.common import MODEL, AgentDeps, check_stock, lookup_item, run_agent, search_inventory
from invoice_agent.events import EventType
from invoice_agent.models import Invoice, ItemMatching, LineCheck, Usage

INSTRUCTIONS = """\
You check invoice lines against Acme Corp's inventory database using the tools.

For every line:
1. Call lookup_item with the item name. A line matches a catalog item only if it names exactly that item,
   ignoring spaces and a trailing parenthetical note such as '(rush order)'.
2. A similar name is NOT a match: 'WidgetC' is not 'WidgetA'. If lookup_item finds nothing, you may call
   search_inventory for a hint, but return sku=null for that line.
3. For matched items, call check_stock with the total quantity of that item across all lines.

Return sku=null whenever you are unsure. Keep notes factual and short.
"""

validation_agent = Agent(
    MODEL,
    name="validation",
    deps_type=AgentDeps,
    output_type=ItemMatching,
    instructions=INSTRUCTIONS,
    tools=[lookup_item, search_inventory, check_stock],
    defer_model_check=True,
)


def match_items(invoice: Invoice, deps: AgentDeps, usage: Usage) -> ItemMatching:
    lines = "\n".join(
        f"{n}. {item.description!r}  quantity={item.quantity}  unit_price={item.unit_price}"
        for n, item in enumerate(invoice.line_items, start=1)
    )
    return run_agent(validation_agent, f"Invoice lines:\n{lines}", deps, usage)


def reconcile(code_lines: list[LineCheck], proposal: ItemMatching, deps: AgentDeps) -> list[str]:
    """Code's catalog rule wins. Each disagreement is recorded so the trace shows what was blocked."""
    proposed = {match.line: match.sku for match in proposal.lines}
    overrides = []
    for check in code_lines:
        agent_sku = proposed.get(check.line)
        if agent_sku == check.sku:
            continue
        if check.sku is None:
            message = f"Line {check.line} {check.description!r}: agent matched it to {agent_sku}; blocked, not an exact catalog name"
        else:
            message = f"Line {check.line} {check.description!r}: agent returned {agent_sku or 'unknown'}; catalog rule matches {check.sku}"
        overrides.append(message)
        deps.sink.emit(deps.stage, EventType.MATCH_OVERRIDE, message, line=check.line, agent_sku=agent_sku, sku=check.sku)
    return overrides
