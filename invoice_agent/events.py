"""Typed pipeline events. Each one goes to two sinks: the SQLite events table and a JSON log line."""

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from invoice_agent import config, db

log = logging.getLogger("invoice_agent.events")


class EventType(StrEnum):
    RUN_STARTED = "run_started"
    STAGE_STARTED = "stage_started"
    STAGE_COMPLETED = "stage_completed"
    TRANSITION = "transition"
    EXTRACTION_RETRY = "extraction_retry"
    TOOL_CALL = "tool_call"
    AGENT_OUTPUT = "agent_output"
    MATCH_OVERRIDE = "match_override"
    FINDING = "finding"
    POLICY = "policy"
    CRITIQUE = "critique"
    POLICY_OVERRIDE = "policy_override"
    DECISION = "decision"
    DUPLICATE = "duplicate"
    REVIEW_REQUESTED = "review_requested"
    REVIEW_RESOLVED = "review_resolved"
    PAYMENT = "payment"
    REJECTION = "rejection"
    ERROR = "error"
    RUN_FINISHED = "run_finished"


class PipelineEvent(BaseModel):
    run_id: int
    ts: str
    stage: str
    type: EventType
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


EventCallback = Callable[[PipelineEvent], None]


class EventSink:
    """Records events for one run and forwards them to an optional live callback (CLI, API)."""

    def __init__(self, run_id: int, on_event: EventCallback | None = None) -> None:
        self.run_id = run_id
        self.on_event = on_event

    def emit(self, stage: str, type_: EventType, message: str, /, **payload: Any) -> PipelineEvent:
        event = PipelineEvent(run_id=self.run_id, ts=now_iso(), stage=stage, type=type_, message=message, payload=payload)
        payload_json = json.dumps(event.payload, default=str)
        db.insert_event(self.run_id, event.ts, stage, type_.value, message, payload_json)
        log.info(event.model_dump_json())
        if self.on_event:
            self.on_event(event)
        return event


def configure_logging() -> None:
    """Events: one JSON object per line in logs/pipeline.jsonl. Tracebacks: logs/errors.log, not the terminal."""
    if any(isinstance(h, logging.FileHandler) for h in log.handlers):
        return
    config.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    events = logging.FileHandler(config.LOG_PATH, encoding="utf-8")
    events.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(events)
    log.setLevel(logging.INFO)
    log.propagate = False

    errors = logging.FileHandler(config.LOG_PATH.with_name("errors.log"), encoding="utf-8")
    errors.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    orchestrator = logging.getLogger("invoice_agent.orchestrator")
    orchestrator.addHandler(errors)
    orchestrator.propagate = False
