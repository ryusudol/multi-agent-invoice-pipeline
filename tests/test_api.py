"""The FastAPI layer: runs in the background worker, polling, review, metrics. Agents replay scripted outputs."""

import io
import threading

import pytest
from fastapi.testclient import TestClient

import api.app as app_module
from api.app import app
from invoice_agent import config, db

from .scripted import ScriptedModels, load_fixture

INVOICES = config.ROOT / "data" / "invoices"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    with ScriptedModels().active() as models, TestClient(app) as client:
        client.models = models  # type: ignore[attr-defined]
        yield client
        app.state.worker.wait()  # finish background runs before tmp_db goes away


def run_sample(client, name: str, folder: str = "invoices") -> dict:
    client.models.load(load_fixture(name))
    started = client.post("/api/runs", data={"sample": f"{folder}/{name}"})
    assert started.status_code == 202, started.text
    app.state.worker.wait()
    return client.get(f"/api/invoices/{started.json()['id']}").json()


def test_samples_list_every_invoice(client):
    ids = [s["id"] for s in client.get("/api/samples").json()]
    assert "invoices/invoice_1001.txt" in ids and "invoices_extra/invoice_2001.txt" in ids
    assert len(ids) == 21


def test_review_from_the_dashboard_pays_what_is_due(client):
    first = run_sample(client, "invoice_1004.json")
    assert first["status"] == "paid"
    revised = run_sample(client, "invoice_1004_revised.json")
    assert revised["status"] == "needs_review"
    assert (revised["total_cents"], revised["due_on_approval_cents"]) == (594000, 405000)
    assert revised["headline"].startswith("Revision of INV-1004")

    paid = client.post(f"/api/invoices/{revised['id']}/review", json={"decision": "approve", "comment": "PO amended"})
    assert paid.status_code == 200, paid.text
    body = paid.json()
    assert body["status"] == "paid"
    assert body["state"]["payment"]["amount_cents"] == 405000
    assert body["state"]["decision"]["review"]["comment"] == "PO amended"
    assert [e["type"] for e in body["events"]][-1] == "run_finished"

    again = client.post(f"/api/invoices/{revised['id']}/review", json={"decision": "approve"})
    assert again.status_code == 409


def test_progress_list_and_metrics(client):
    rush = run_sample(client, "invoice_1010.txt")
    run_sample(client, "invoice_1011.pdf")
    run_sample(client, "invoice_1011.txt")
    run_sample(client, "invoice_1016.json")

    progress = client.get(f"/api/runs/{rush['id']}").json()
    assert (progress["status"], progress["stage"]) == ("needs_review", "needs_review")
    assert [r["file"] for r in client.get("/api/invoices").json()] == [
        "invoice_1016.json", "invoice_1011.txt", "invoice_1011.pdf", "invoice_1010.txt",
    ]  # fmt: skip
    assert [r["id"] for r in client.get("/api/invoices", params={"status": "needs_review"}).json()] == [rush["id"]]

    processed = [s["name"] for s in client.get("/api/samples").json() if s["processed"]]
    assert processed == ["invoice_1010.txt", "invoice_1011.pdf", "invoice_1011.txt", "invoice_1016.json"]

    m = client.get("/api/metrics").json()
    assert m["by_status"] == {"needs_review": 1, "paid": 1, "duplicate": 1, "rejected": 1}
    assert m["findings_by_code"]["unknown_item"] == 1


def test_upload_runs_the_file(client):
    client.models.load(load_fixture("invoice_1001.txt"))
    content = (INVOICES / "invoice_1001.txt").read_bytes()
    started = client.post("/api/runs", files={"file": ("invoice_1001.txt", io.BytesIO(content), "text/plain")})
    assert started.status_code == 202, started.text
    app.state.worker.wait()
    done = client.get(f"/api/invoices/{started.json()['id']}").json()
    assert (done["status"], done["state"]["payment"]["amount_cents"]) == ("paid", 500000)


@pytest.mark.parametrize(
    ("kwargs", "status"),
    [
        ({"data": {"sample": "../../README.md"}}, 404),
        ({"files": {"file": ("notes.exe", io.BytesIO(b"x"), "application/octet-stream")}}, 415),
        ({}, 422),
    ],
)
def test_bad_run_requests(client, kwargs, status):
    assert client.post("/api/runs", **kwargs).status_code == status


def test_unknown_invoice_is_404(client):
    assert client.get("/api/invoices/99").status_code == 404
    assert client.post("/api/invoices/99/review", json={"decision": "approve"}).status_code == 404


def test_stopping_the_server_fails_queued_runs(monkeypatch):
    """Ctrl-C on serve finishes the run in flight and does not work through the rest of the queue."""
    started, release = threading.Event(), threading.Event()

    def slow_run(path, on_event=None, run_id=None):
        started.set()
        release.wait(5)
        db.update_run(run_id, status="paid", stage="paid")

    monkeypatch.setattr(app_module, "run_pipeline", slow_run)
    with TestClient(app) as client:
        ids = [client.post("/api/runs", data={"sample": "invoices/invoice_1001.txt"}).json()["id"] for _ in range(3)]
        assert started.wait(5)
        assert client.get(f"/api/runs/{ids[2]}").json()["stage"] == "queued"
        threading.Timer(0.2, release.set).start()
    rows = [db.get_run(run_id) for run_id in ids]
    assert [r["status"] for r in rows] == ["paid", "failed", "failed"]
    assert rows[1]["error"] == "The server stopped before this run started"
