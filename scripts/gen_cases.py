"""Regenerate evals/cases.jsonl from the fixture incidents.

Keeps the benchmark labels in lockstep with the tool signal (one source of
truth in sre_triage/fixtures.py). Run after editing fixtures:

    python scripts/gen_cases.py
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sre_triage.fixtures import INCIDENTS  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parent.parent / "evals" / "cases.jsonl"


def main() -> int:
    rows = []
    for inc in INCIDENTS:
        rows.append(
            {
                "id": inc["id"],
                "service": inc["service"],
                "window": inc["window"],
                "difficulty": inc["difficulty"],
                "scenario": inc["scenario"],
                "expected_escalate_to": inc["label"]["expected_escalate_to"],
                "expected_root_cause": inc["label"]["expected_root_cause"],
            }
        )
    OUT.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    print(f"wrote {len(rows)} cases -> {OUT.relative_to(OUT.parent.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
