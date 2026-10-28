"""Read any invoice file as raw text. The Intake agent handles every format the same way."""

from pathlib import Path

import pdfplumber

from invoice_agent import config


class UnsupportedFileError(ValueError):
    pass


def is_invoice(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in config.SUPPORTED_SUFFIXES


def invoice_files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if is_invoice(p))


def load_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in config.SUPPORTED_SUFFIXES:
        raise UnsupportedFileError(f"{path.name}: unsupported file type (expected {', '.join(config.SUPPORTED_SUFFIXES)})")
    if suffix == ".pdf":
        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        raise UnsupportedFileError(f"{path.name}: no text could be read")
    return text
