"""Turn a live run's recorded agent outputs into golden-test fixtures.

    uv run python main.py --reset --invoice_path=data/invoices/
    uv run python main.py --invoice_path=data/invoices_extra/
    uv run python -m tests.record_fixtures

Writes tests/fixtures/<file name>.json with each agent's outputs in call order, taken from the
`agent_output` events of the latest non-failed run of each file. Review the files before committing:
the live test compares real Grok extractions against them.
"""

import json
import sys
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from invoice_agent import db

FIXTURES = Path(__file__).parent / "fixtures"


def main(db_path: str | None = None) -> None:
    FIXTURES.mkdir(exist_ok=True)
    with closing(db.connect(Path(db_path) if db_path else None)) as conn:
        runs = conn.execute("SELECT id, source_file FROM runs WHERE status != 'failed' ORDER BY id").fetchall()
        latest = {Path(row["source_file"]).name: row["id"] for row in runs}
        for name, run_id in sorted(latest.items()):
            outputs: dict[str, list] = defaultdict(list)
            for event in conn.execute(
                "SELECT payload FROM events WHERE run_id = ? AND type = 'agent_output' ORDER BY id", (run_id,)
            ):
                payload = json.loads(event["payload"])
                outputs[payload["agent"]].append(payload["output"])
            path = FIXTURES / f"{name}.json"
            path.write_text(json.dumps(outputs, indent=2, ensure_ascii=False) + "\n")
            print(f"{path.relative_to(FIXTURES.parent.parent)}: " + ", ".join(f"{k}×{len(v)}" for k, v in outputs.items()))


if __name__ == "__main__":
    main(*sys.argv[1:])
