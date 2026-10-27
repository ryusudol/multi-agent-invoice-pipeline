"""FunctionModels that replay scripted agent outputs, so the full pipeline runs offline."""

import json
from collections import deque
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from invoice_agent.agents.approval import critic_agent, vp_agent
from invoice_agent.agents.intake import intake_agent
from invoice_agent.agents.validation import validation_agent

AGENTS = (intake_agent, validation_agent, vp_agent, critic_agent)
FIXTURES = Path(__file__).parent / "fixtures"


class ScriptedModels:
    """One queue of outputs per agent. Each model request pops the next output for that agent."""

    def __init__(self) -> None:
        self.queues: dict[str, deque[dict[str, Any]]] = {agent.name or "": deque() for agent in AGENTS}

    def load(self, script: dict[str, list[dict[str, Any]]]) -> None:
        for name, outputs in script.items():
            self.queues[name] = deque(outputs)

    def leftovers(self) -> dict[str, int]:
        return {name: len(queue) for name, queue in self.queues.items() if queue}

    def _model(self, name: str) -> FunctionModel:
        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            if not self.queues[name]:
                raise AssertionError(f"no scripted output left for the {name} agent")
            output = self.queues[name].popleft()
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output)])

        return FunctionModel(respond, model_name=f"scripted-{name}")

    @contextmanager
    def active(self) -> Iterator["ScriptedModels"]:
        with ExitStack() as stack:
            for agent in AGENTS:
                stack.enter_context(agent.override(model=self._model(agent.name or "")))
            yield self


def load_fixture(file_name: str) -> dict[str, list[dict[str, Any]]]:
    return json.loads((FIXTURES / f"{file_name}.json").read_text())
