# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Multi-Agent Invoice Pipeline: Grok agents extract, validate, and approve invoices, and code enforces policy and pays or rejects. `README.md` covers running it. The expected outcome for every sample invoice is `ANSWER_KEY` in `tests/test_golden.py`.

## Commands

Python 3.14 with uv. Grok is reached through Pydantic AI; `XAI_API_KEY` (and optionally `XAI_MODEL`) go in `.env` (`cp .env.example .env`). The CLI exits if the key is missing.

```bash
uv sync
uv run python main.py --invoice_path=data/invoices/invoice_1001.txt   # one file
uv run python main.py --invoice_path=data/invoices/                   # folder, filename order, summary table
uv run python main.py --reset --invoice_path=...                      # wipe and re-seed inventory.db first
uv run python main.py -v --invoice_path=...                           # show LLM and tool calls
uv run python main.py serve [--port 8000]                             # FastAPI + built dashboard

uv run pytest                                  # offline suite (live tests excluded by default)
uv run pytest tests/test_rules.py::test_clean_invoice_has_no_findings   # single test
uv run pytest -m live                          # real Grok extraction vs fixtures; needs the key
```

Frontend (`web/`, React + Vite + Tailwind + TanStack Query):

```bash
cd web && npm install
npm run dev     # Vite dev server; proxies /api to 127.0.0.1:8000, so run `main.py serve` alongside
npm run build   # tsc --noEmit, then writes web/dist
```

`web/dist` is committed on purpose so the dashboard runs without Node. Rebuild it after any change under `web/src`.

## Architecture

**Principle: the LLM reads, code decides.** LLM agents interpret documents, propose item matches, and draft approval rationale. Deterministic code does arithmetic, stock checks, duplicate detection, and policy, and it always overrides the LLM:
- `ingestion/check.py` is the Intake agent's output validator. Values not found in the source text trigger a `ModelRetry` (up to `EXTRACTION_RETRIES`). Real errors in the document, like a wrong total, are not retried; Validation flags them.
- `agents/validation.py:reconcile` keeps the code's exact-catalog match over the agent's proposal and records each disagreement as a `match_override` event. For example, WidgetC must never map to WidgetA.
- `approval/policy.py` turns findings into a `PolicyMode` (reject / human_review / strict / standard). `agents/approval.py:_enforce` stops the VP from changing a fixed outcome, and in standard mode a VP reject is downgraded to needs_review.

**Orchestrator (`invoice_agent/orchestrator.py`).** A typed state machine: a `Stage` enum, a `TRANSITIONS` table keyed by `(stage, condition)`, and a `HANDLERS` dict. Each handler mutates `RunState` and returns `(condition, one-line summary)`. Any exception sends the run to `FAILED`. `RunState` is saved to the `runs` table before every stage. A run that stops at `needs_review` can therefore be resumed by `resolve_review()` from another process (CLI prompt or dashboard). `run_pipeline()` and `resolve_review()` are the only two entry points, and both `main.py` and `api/app.py` use them. To add a stage, add a `Stage` member, a handler, and its transitions.

**Agents (`invoice_agent/agents/`).** Four Pydantic AI agents: intake, validation (with read-only inventory tools), VP, and critic. All use `MODEL = "xai:<XAI_MODEL>"` with `defer_model_check=True`, so importing them needs no key. Always call them through `common.run_agent`, which adds token usage to the run and emits an `agent_output` event. The approval critique loop runs `MAX_CRITIQUE_ROUNDS` rounds in strict mode and 1 round otherwise. If the VP and critic never agree, the run escalates to needs_review.

**Events and persistence.** `EventSink.emit` writes every event to the SQLite `events` table and to `logs/pipeline.jsonl`; tracebacks go to `logs/errors.log`. The dashboard's trace view is built from these events, and so are the test fixtures. `inventory.db` sits at the repo root. It is created and seeded on first run and opened in WAL mode, so the CLI and API can share it. Money is stored as integer cents. The invoice's stated amounts and the code's computed amounts are kept as separate fields.

**Duplicates and payment.** `duplicates.py` identifies an invoice by its content, not its file. The key is the normalized number plus vendor, and the fingerprint hashes the core fields, so a TXT and a PDF of the same invoice count as identical. The same key with different content goes to human review. `payment.py` pays only the approved total minus what is already paid under the same key, behind an idempotency key.

**API (`api/app.py`).** Runs execute on a single background worker thread in submission order, so each duplicate check sees the earlier runs. `Worker.submit` copies contextvars so that test model overrides reach that thread. The API also serves `web/dist` as static files.

## Tests

- `tests/conftest.py` sets `pydantic_ai.models.ALLOW_MODEL_REQUESTS = False` and gives every test a fresh temp DB (it patches `config.DB_PATH`).
- `tests/scripted.py:ScriptedModels` overrides all four agents with `FunctionModel`s. Each agent replays a queue of outputs, loaded per invoice from `tests/fixtures/<file name>.json`.
- `tests/test_golden.py` runs every sample invoice end to end, offline. `ANSWER_KEY` maps each file to its expected status, amount paid, and finding codes.
- If you change an agent's output schema or prompt so that recorded outputs no longer fit, re-record the fixtures from a live run, then review the diff:
  ```bash
  uv run python main.py --reset --invoice_path=data/invoices/
  uv run python main.py --invoice_path=data/invoices_extra/
  uv run python -m tests.record_fixtures
  ```

## Notes

- Policy thresholds and retry/round caps live in `invoice_agent/config.py`.
- `.claude/settings.json` denies reading `.env*` files.
- `invoice_agent/` must not import FastAPI or web code; `api/` is a thin layer over it.
