from pathlib import Path

import pytest

import main
from invoice_agent import config
from invoice_agent.models import RunStatus
from invoice_agent.orchestrator import load_state

from .scripted import ScriptedModels
from .test_orchestrator import AGREE, INTAKE_1010, matching, vp

INVOICES = config.ROOT / "data" / "invoices"
SCRIPT_1010 = {"intake": [INTAKE_1010], "validation": [matching("WidgetA", "WidgetB", "GadgetX", "WidgetA")], "vp": [vp("needs_review")], "critic": [AGREE]}


@pytest.fixture(autouse=True)
def fake_key(monkeypatch):
    monkeypatch.setattr(config, "xai_api_key", lambda: "test-key")
    monkeypatch.setattr(main, "configure_logging", lambda: None)


@pytest.fixture
def answers(monkeypatch):
    """Simulate a terminal and queue answers for Rich prompts."""
    queue: list = []
    monkeypatch.setattr(main.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("invoice_agent.console.Prompt.ask", lambda *a, **k: queue.pop(0))
    monkeypatch.setattr("invoice_agent.console.Confirm.ask", lambda *a, **k: queue.pop(0))
    return queue


def test_missing_key_explains_setup(monkeypatch, capsys):
    monkeypatch.setattr(config, "xai_api_key", lambda: None)
    assert main.main(["--invoice_path", str(INVOICES / "invoice_1001.txt")]) == 1
    assert "XAI_API_KEY is not set" in capsys.readouterr().out


def test_wrong_path_suggests_a_file(capsys):
    assert main.main(["--invoice_path", str(INVOICES / "invoice_1001.tx")]) == 2
    out = capsys.readouterr().out.replace("\n", "")
    assert "does not exist" in out
    assert "Did you mean" in out and out.rstrip().endswith("invoice_1001.txt?")


@pytest.mark.parametrize(
    ("typed", "suggested"),
    [
        ("data/invoices/invoice_1010.tx", "data/invoices/invoice_1010.txt"),  # typo in the name
        ("data/invoice/invoice_1010.txt", "data/invoices/invoice_1010.txt"),  # typo in the folder
        ("data/invoicez", "data/invoices"),  # a folder to process in batch
        ("invoice_1010.txt", "data/invoices/invoice_1010.txt"),  # right name, wrong folder
        ("invoice_1010", "data/invoices/invoice_1010.txt"),  # not the invoice_agent package
        ("data/invoices/zzz", None),
    ],
)
def test_suggest_path(monkeypatch, typed, suggested):
    monkeypatch.chdir(config.ROOT)
    assert main.suggest_path(Path(typed)) == suggested


def test_review_prompt_approves_and_pays(answers, capsys):
    answers += ["approve", "Rush order agreed", True]
    with ScriptedModels().active() as models:
        models.load(SCRIPT_1010)
        assert main.main(["--invoice_path", str(INVOICES / "invoice_1010.txt")]) == 0
    assert load_state(1).status == RunStatus.PAID
    assert "Paid $7,185.00 to Consolidated Materials Group" in capsys.readouterr().out


def test_review_can_be_skipped(answers):
    answers += ["skip"]
    with ScriptedModels().active() as models:
        models.load(SCRIPT_1010)
        main.main(["--invoice_path", str(INVOICES / "invoice_1010.txt")])
    assert load_state(1).status == RunStatus.NEEDS_REVIEW


def test_no_prompt_outside_a_terminal(monkeypatch):
    monkeypatch.setattr(main.sys.stdin, "isatty", lambda: False)
    with ScriptedModels().active() as models:
        models.load(SCRIPT_1010)
        main.main(["--invoice_path", str(INVOICES / "invoice_1010.txt")])
    assert load_state(1).status == RunStatus.NEEDS_REVIEW


def test_rerun_of_pending_invoice_offers_review_again(answers, capsys):
    with ScriptedModels().active() as models:
        models.load(SCRIPT_1010)
        answers += ["skip"]
        main.main(["--invoice_path", str(INVOICES / "invoice_1010.txt")])
        models.load({"intake": [INTAKE_1010]})
        answers += ["reject", "Not agreed"]
        main.main(["--invoice_path", str(INVOICES / "invoice_1010.txt")])
    out = capsys.readouterr().out
    assert "DUPLICATE" in out
    assert load_state(1).status == RunStatus.REJECTED
    assert load_state(2).status == RunStatus.DUPLICATE


def test_rationale_bullets_do_not_split_after_a_company_suffix():
    from invoice_agent.console import SENTENCE_END

    text = "History run #4 paid Precision Parts Ltd. $1,890.00 on this number. Total is $5,940.00."
    assert SENTENCE_END.split(text) == ["History run #4 paid Precision Parts Ltd. $1,890.00 on this number.", "Total is $5,940.00."]
