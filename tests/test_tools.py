"""Agent tools run for real: the model calls them, Pydantic AI dispatches them, and each call is traced.

The scripted fixtures skip straight to final outputs, so this is the only offline coverage of the tools.
"""

import json

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from invoice_agent import db
from invoice_agent.agents.approval import vp_agent
from invoice_agent.agents.common import AgentDeps, run_agent
from invoice_agent.agents.validation import validation_agent
from invoice_agent.events import EventSink, now_iso
from invoice_agent.models import Usage


def tool_then_output(calls: list[tuple[str, dict]], output: dict, returns: dict) -> FunctionModel:
    """First request: call the tools. Second request: record what they returned, then give the output."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart(name, args) for name, args in calls])
        returns.update({p.tool_name: p.content for p in messages[-1].parts if isinstance(p, ToolReturnPart)})
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output)])

    return FunctionModel(respond)


def deps(catalog) -> AgentDeps:
    return AgentDeps(sink=EventSink(db.create_run("invoice.txt", now_iso())), stage="validate", catalog=catalog)


def tool_events(run_id: int) -> list[dict]:
    return [json.loads(row["payload"]) for row in db.list_events(run_id) if row["type"] == "tool_call"]


def test_validation_tools_return_catalog_data(catalog):
    calls = [
        ("lookup_item", {"name": "Widget A"}),
        ("lookup_item", {"name": "WidgetC"}),
        ("search_inventory", {"query": "WidgetC"}),
        ("check_stock", {"item": "WidgetA", "quantity": 20}),
    ]
    output = {"lines": [{"line": 1, "sku": "WidgetA", "note": "checked"}], "summary": "checked"}
    returns: dict = {}
    agent_deps, usage = deps(catalog), Usage()
    with validation_agent.override(model=tool_then_output(calls, output, returns)):
        run_agent(validation_agent, "Invoice lines", agent_deps, usage)

    assert returns["search_inventory"]["similar_names"][0] == "WidgetA"
    assert returns["check_stock"] == {"found": True, "item": "WidgetA", "stock": 15, "sufficient": False}
    assert usage.tool_calls == 4
    # Tool calls in one response run concurrently, so events can land in any order.
    events = tool_events(agent_deps.sink.run_id)
    assert sorted(e["tool"] for e in events) == ["check_stock", "lookup_item", "lookup_item", "search_inventory"]
    lookups = {e["args"]["name"]: e["result"] for e in events if e["tool"] == "lookup_item"}
    assert lookups["Widget A"]["found"] is True
    assert lookups["WidgetC"] == {"found": False, "item": "WidgetC"}


def test_invoice_history_excludes_the_current_run(catalog):
    earlier = db.create_run("invoice_1001.txt", now_iso())
    db.update_run(earlier, invoice_number="INV-1001", vendor="Widgets Inc.", status="paid", total_cents=500000, currency="USD")
    output = {"decision": "approve", "rationale": "Evidence supports this.", "checklist_responses": []}
    returns: dict = {}
    agent_deps = deps(catalog)
    with vp_agent.override(model=tool_then_output([("get_invoice_history", {"invoice_number": "INV 1001"})], output, returns)):
        run_agent(vp_agent, "Case file", agent_deps, Usage())

    history = returns["get_invoice_history"]
    assert history["number"] == "INV-1001"
    assert [run["run_id"] for run in history["runs"]] == [earlier]
