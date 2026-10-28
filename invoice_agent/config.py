"""Settings from .env, policy thresholds, and fixed paths."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DB_PATH = ROOT / "inventory.db"
LOG_PATH = ROOT / "logs" / "pipeline.jsonl"
UPLOAD_DIR = ROOT / "runs" / "uploads"
SAMPLE_DIRS = (ROOT / "data" / "invoices", ROOT / "data" / "invoices_extra")
SUPPORTED_SUFFIXES = (".txt", ".json", ".csv", ".xml", ".pdf")

XAI_MODEL = os.getenv("XAI_MODEL") or "grok-4.7"
AGENT_TIMEOUT_SECONDS = 180

# Invoices over $10K get strict review.
STRICT_REVIEW_THRESHOLD_CENTS = 10_000_00
# Stated vs computed amounts may differ by at most this much (rounding).
MONEY_TOLERANCE_CENTS = 1

# Extraction retries after a schema, parse, or source-match failure.
EXTRACTION_RETRIES = 2
# VP draft + Critic review rounds for strict review before escalating to a human.
MAX_CRITIQUE_ROUNDS = 3


def xai_api_key() -> str | None:
    return os.getenv("XAI_API_KEY") or None
