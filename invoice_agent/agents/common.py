"""Shared agent plumbing: dependencies, read-only tools, and a runner that records output and usage."""

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext

from invoice_agent import config, db
from invoice_agent.events import EventSink, EventType
from invoice_agent.ingestion.normalize import format_money, item_key, normalize_invoice_number
from invoice_agent.models import Usage
from invoice_agent.validation.rules import suggest

# Resolved at run time, so importing agents never needs an API key. Tests swap it with Agent.override().
MODEL = f"xai:{config.XAI_MODEL}"


@dataclass
class AgentDeps:
    sink: EventSink
    stage: str
    source_text: str = ""
    catalog: dict[str, Any] = field(default_factory=dict)


def run_agent[T: BaseModel](agent: Agent[AgentDeps, T], prompt: str, deps: AgentDeps, usage: Usage) -> T:
    result = agent.run_sync(prompt, deps=deps, timeout=config.AGENT_TIMEOUT_SECONDS)
    run_usage = result.usage
    used = Usage(
        requests=run_usage.requests,
        input_tokens=run_usage.input_tokens,
        output_tokens=run_usage.output_tokens,
        tool_calls=run_usage.tool_calls,
    )
    usage.add(used)
    deps.sink.emit(
        deps.stage,
        EventType.AGENT_OUTPUT,
        f"{agent.name} agent responded",
        agent=agent.name,
        output=result.output.model_dump(mode="json"),
        usage=used.model_dump(),
    )
    return result.output


def _record(ctx: RunContext[AgentDeps], tool: str, args: dict[str, Any], result: Any) -> Any:
    shown_args = ", ".join(f"{v!r}" for v in args.values())
    ctx.deps.sink.emit(
        ctx.deps.stage,
        EventType.TOOL_CALL,
        f"{tool}({shown_args}) → {json.dumps(result, default=str)}",
        tool=tool,
        args=args,
        result=result,
    )
    return result


def _catalog_entry(sku: str, row: Any) -> dict[str, Any]:
    price = row["unit_price"]
    return {"item": sku, "stock": row["stock"], "catalog_unit_price": price}


def lookup_item(ctx: RunContext[AgentDeps], name: str) -> dict[str, Any]:
    """Look up one inventory item by its exact catalog name (spaces are ignored). Returns stock and catalog price, or found=false."""
    catalog = ctx.deps.catalog
    sku = item_key(name)
    row = catalog.get(sku)
    result = {"found": True, **_catalog_entry(sku, row)} if row is not None else {"found": False, "item": name}
    return _record(ctx, "lookup_item", {"name": name}, result)


def search_inventory(ctx: RunContext[AgentDeps], query: str) -> dict[str, Any]:
    """List catalog items whose names look similar to the query. Similar is not the same: use it for hints only."""
    catalog = ctx.deps.catalog
    result = {
        "query": query,
        "similar_names": suggest(query, catalog),
        "catalog": [_catalog_entry(sku, row) for sku, row in catalog.items()],
    }
    return _record(ctx, "search_inventory", {"query": query}, result)


def check_stock(ctx: RunContext[AgentDeps], item: str, quantity: float) -> dict[str, Any]:
    """Check whether the total quantity requested for a catalog item is available in stock."""
    catalog = ctx.deps.catalog
    row = catalog.get(item_key(item))
    if row is None:
        result: dict[str, Any] = {"found": False, "item": item}
    else:
        result = {"found": True, "item": item_key(item), "stock": row["stock"], "sufficient": quantity <= row["stock"]}
    return _record(ctx, "check_stock", {"item": item, "quantity": quantity}, result)


def get_invoice_history(ctx: RunContext[AgentDeps], invoice_number: str) -> dict[str, Any]:
    """Earlier processing runs and payments for an invoice number (any format, e.g. 'INV 1004')."""
    number = normalize_invoice_number(invoice_number)
    rows = db.runs_for_number(number) if number else []
    history = [
        {
            "run_id": row["id"],
            "file": row["source_file"],
            "status": row["status"],
            "vendor": row["vendor"],
            "total": format_money(row["total_cents"], row["currency"] or "USD"),
        }
        for row in rows
        if row["id"] != ctx.deps.sink.run_id
    ]
    return _record(ctx, "get_invoice_history", {"invoice_number": invoice_number}, {"number": number, "runs": history})
