"""Scaffold new (predicted, expected) pairs for a human to label.

Runs an agent over the benchmark, pairs each predicted root_cause with the
ground-truth root_cause, and writes rows with ``human_verdict`` left blank for
YOU to fill in. It never fills the verdict itself: the whole value of
``human_labels.jsonl`` is that a *human* graded those rows, so a machine-written
verdict would make the judge validation circular and worthless.

    # generate pairs from the mock agent (offline, deterministic)
    python scripts/make_label_pairs.py --model mock --out evals/labels_todo.jsonl

    # or from real Claude, for pairs closer to what the judge actually sees
    python scripts/make_label_pairs.py --model anthropic --out evals/labels_todo.jsonl

Then, for each row, set ``human_verdict`` to one of correct | partial | wrong by
your own judgment, and append the finished rows to evals/human_labels.jsonl.

Skips signal cases only (root_cause quality is not judged on NO_DATA cases — the
judge special-cases those), and skips pairs whose (predicted, expected) already
appear in human_labels.jsonl so you only label genuinely new material.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sre_triage.agent import diagnose  # noqa: E402
from sre_triage.fixtures import by_id  # noqa: E402
from sre_triage.model import get_backend  # noqa: E402
from sre_triage.schemas import NO_DATA  # noqa: E402
from sre_triage.tools import set_active_incident  # noqa: E402

CASES = ROOT / "evals" / "cases.jsonl"
LABELS = ROOT / "evals" / "human_labels.jsonl"


def _existing_pairs() -> set[tuple[str, str]]:
    if not LABELS.exists():
        return set()
    seen = set()
    for line in LABELS.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            seen.add((r["predicted_root_cause"], r["expected_root_cause"]))
    return seen


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Scaffold root_cause pairs for human labeling")
    p.add_argument("--model", default="mock", choices=["anthropic", "mock"])
    p.add_argument("--out", default=str(ROOT / "evals" / "labels_todo.jsonl"))
    p.add_argument("--cases", default=str(CASES))
    args = p.parse_args(argv)

    cases = [json.loads(l) for l in pathlib.Path(args.cases).read_text().splitlines() if l.strip()]
    try:
        backend = get_backend(args.model)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    seen = _existing_pairs()
    rows, skipped_known, skipped_nodata = [], 0, 0
    for case in cases:
        if case["expected_root_cause"] == NO_DATA:
            skipped_nodata += 1
            continue
        set_active_incident(by_id(case["id"]))
        diag = diagnose(case["scenario"], case["service"], case["window"], backend=backend)
        pair = (diag.root_cause, case["expected_root_cause"])
        if pair in seen:
            skipped_known += 1
            continue
        seen.add(pair)
        rows.append(
            {
                "predicted_root_cause": diag.root_cause,
                "expected_root_cause": case["expected_root_cause"],
                "human_verdict": "",  # <- YOU fill this: correct | partial | wrong
                "_case_id": case["id"],  # provenance; drop before appending to human_labels
            }
        )

    out = pathlib.Path(args.out)
    out.write_text("\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""))
    print(f"wrote {len(rows)} unlabeled pairs -> {out}")
    print(f"  skipped {skipped_known} already in human_labels.jsonl, {skipped_nodata} NO_DATA cases")
    if rows:
        print("\nFill in each human_verdict (correct | partial | wrong), drop _case_id,")
        print("then append the finished rows to evals/human_labels.jsonl.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
