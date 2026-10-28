"""FastAPI over the same core the CLI uses: run_pipeline() for new invoices, resolve_review() for reviews.

Runs go to one background thread in submission order, so a duplicate check always sees the earlier invoices.
The dashboard polls for progress; every stage is saved to SQLite as it finishes.
"""

import contextvars
import getpass
import json
import uuid
from collections.abc import AsyncIterator
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from invoice_agent import config, db
from invoice_agent.events import PipelineEvent, now_iso
from invoice_agent.ingestion.loader import is_invoice
from invoice_agent.ingestion.normalize import due_date_text, normalize_invoice_number
from invoice_agent.models import FindingCode, RunStatus
from invoice_agent.orchestrator import (
    ReviewError,
    RunState,
    Stage,
    create_run,
    due_on_approval,
    load_state,
    resolve_review,
    run_pipeline,
    state_from_row,
)

WEB_DIST = config.ROOT / "web" / "dist"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class Worker:
    """A single pipeline thread. Pending runs wait their turn with status processing, stage queued."""

    def __init__(self) -> None:
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline")
        self.pending: dict[Future, int] = {}

    def submit(self, path: Path, run_id: int) -> None:
        context = contextvars.copy_context()  # carries agent overrides (scripted models in tests) into the thread
        future = self.pool.submit(context.run, run_pipeline, path, None, run_id)
        self.pending[future] = run_id
        future.add_done_callback(lambda f: self.pending.pop(f, None))

    def wait(self) -> None:
        wait(list(self.pending))

    def stop(self) -> None:
        """Finish the run in flight; fail the queued ones so they don't show as queued forever."""
        queued = [run_id for future, run_id in list(self.pending.items()) if future.cancel()]
        self.pool.shutdown(wait=True)
        for run_id in queued:
            db.update_run(
                run_id, status=RunStatus.FAILED.value, stage=Stage.FAILED.value,
                error="The server stopped before this run started", finished_at=now_iso(),
            )  # fmt: skip


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.worker = Worker()
    yield
    app.state.worker.stop()


class Sample(BaseModel):
    id: str  # folder/name, e.g. invoices/invoice_1001.txt
    name: str
    folder: str
    processed: bool  # some run already used this file


class RunSummary(BaseModel):
    id: int
    file: str
    invoice_number: str | None
    vendor: str | None
    total_cents: int | None
    currency: str
    status: RunStatus
    stage: Stage
    headline: str
    started_at: str
    duration_ms: int


class Event(PipelineEvent):
    id: int


class RunDetail(RunSummary):
    state: RunState
    due_date: str | None  # ISO when it parses, like the CLI card
    due_on_approval_cents: int | None
    events: list[Event]


class ReviewRequest(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = ""


class Metrics(BaseModel):
    by_status: dict[RunStatus, int]
    findings_by_code: dict[FindingCode, int]  # invoices with at least one finding of that kind


def headline(state: RunState) -> str:
    """One line for the list: why it ended where it did."""
    if state.status == RunStatus.DUPLICATE and state.duplicate:
        return f"Same as {state.duplicate.earlier} ({state.duplicate.earlier_status})"
    if state.error:
        return state.error
    if state.payment:
        return "" if state.payment.amount_cents == state.total_cents else "Paid the difference from an earlier version"
    if state.decision and state.decision.reasons:
        return state.decision.reasons[0]
    return ""


def summarize(state: RunState) -> RunSummary:
    invoice = state.invoice
    return RunSummary(
        id=state.run_id,
        file=state.name,
        invoice_number=normalize_invoice_number(invoice.invoice_number) if invoice else None,
        vendor=invoice.vendor if invoice else None,
        total_cents=state.total_cents,
        currency=state.currency,
        status=state.status,
        stage=state.stage,
        headline=headline(state),
        started_at=state.started_at,
        duration_ms=state.duration_ms,
    )


def detail(state: RunState) -> RunDetail:
    due = due_on_approval(state) if state.status == RunStatus.NEEDS_REVIEW else None
    events = [Event.model_validate({**e, "payload": json.loads(e["payload"])}) for e in map(dict, db.list_events(state.run_id))]
    due_date = due_date_text(state.invoice.due_date) if state.invoice else None
    return RunDetail(**summarize(state).model_dump(), state=state, due_date=due_date, due_on_approval_cents=due, events=events)


def sample_paths() -> dict[str, Path]:
    """id (folder/name) -> path, for every invoice in the sample folders."""
    return {
        f"{folder.name}/{path.name}": path
        for folder in config.SAMPLE_DIRS
        if folder.is_dir()
        for path in sorted(folder.iterdir())
        if is_invoice(path)
    }


def samples() -> list[Sample]:
    used = {(config.ROOT / file).resolve() for file in db.source_files()}  # CLI paths are relative to the repo
    return [
        Sample(id=sample_id, name=path.name, folder=path.parent.name, processed=path.resolve() in used)
        for sample_id, path in sample_paths().items()
    ]


def get_state(run_id: int) -> RunState:
    try:
        return load_state(run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


app = FastAPI(title="Acme invoice agent", lifespan=lifespan)


@app.get("/api/samples")
def list_samples() -> list[Sample]:
    return samples()


@app.post("/api/runs", status_code=202)
def start_run(file: UploadFile | None = File(None), sample: str | None = Form(None)) -> RunSummary:
    if (file is None) == (sample is None):
        raise HTTPException(422, "Send either a file or a sample")
    if sample is not None:
        found = sample_paths().get(sample)
        if found is None:
            raise HTTPException(404, f"No sample named {sample}")
        path = found
    else:
        assert file is not None
        name = Path(file.filename or "").name
        if Path(name).suffix.lower() not in config.SUPPORTED_SUFFIXES:
            raise HTTPException(415, f"Supported files: {', '.join(config.SUPPORTED_SUFFIXES)}")
        content = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Files up to 10 MB")
        path = config.UPLOAD_DIR / uuid.uuid4().hex / name
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
    run_id = create_run(path)
    app.state.worker.submit(path, run_id)
    return summarize(load_state(run_id))


@app.get("/api/runs/{run_id}")
def run_progress(run_id: int) -> RunSummary:
    return summarize(get_state(run_id))


@app.get("/api/invoices")
def list_invoices(status: RunStatus | None = None) -> list[RunSummary]:
    return [summarize(state_from_row(row)) for row in db.list_runs(status.value if status else None)]


@app.get("/api/invoices/{run_id}")
def invoice_detail(run_id: int) -> RunDetail:
    return detail(get_state(run_id))


@app.post("/api/invoices/{run_id}/review")
def review(run_id: int, body: ReviewRequest) -> RunDetail:
    try:
        state = resolve_review(run_id, body.decision, body.comment.strip(), reviewer=getpass.getuser())
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ReviewError as exc:
        raise HTTPException(409, str(exc)) from exc
    return detail(state)


@app.get("/api/metrics")
def metrics() -> Metrics:
    return Metrics.model_validate({"by_status": db.status_counts(), "findings_by_code": db.finding_counts()})


if WEB_DIST.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
