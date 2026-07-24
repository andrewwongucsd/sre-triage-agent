"""Pick the judge by measurement instead of assumption.

Runs each candidate judge over the hand-labeled examples in
``evals/human_labels.jsonl`` and reports agreement + Cohen's κ against the human
verdicts, so the choice of judge model is an evidence-backed decision rather
than a default nobody revisited.

    python scripts/compare_judges.py                      # mock only, offline
    python scripts/compare_judges.py --models claude-opus-4-8,claude-sonnet-5

Why it matters here: ``make eval-real`` runs the same model as agent *and*
judge, which is the standard criticism of LLM-as-judge — shared blind spots.
This is how you find out whether an independent judge is actually better, and
by how much, before changing the default.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evals.judge import AnthropicJudge, MockJudge, validate_judge  # noqa: E402

LABELS = ROOT / "evals" / "human_labels.jsonl"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Compare judge models against human labels")
    p.add_argument(
        "--models",
        default="",
        help="comma-separated Anthropic model ids to evaluate as judges "
        "(needs ANTHROPIC_API_KEY); the offline heuristic judge always runs",
    )
    p.add_argument("--labels", default=str(LABELS))
    args = p.parse_args(argv)

    candidates: list[tuple[str, object]] = [("mock (keyword heuristic)", MockJudge())]
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        try:
            candidates.append((model, AnthropicJudge(model=model)))
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    rows = []
    for name, judge in candidates:
        v = validate_judge(judge, args.labels)
        rows.append((name, v))

    n = rows[0][1]["n"]
    print(f"## Judge comparison ({n} hand-labeled examples)\n")
    print("| judge | agreement | Cohen's κ | disagreements |")
    print("| --- | --- | --- | --- |")
    for name, v in rows:
        print(
            f"| {name} | {v['agreement']:.1%} | {v['cohen_kappa']} | "
            f"{len(v['disagreements'])}/{v['n']} |"
        )

    best_name, best_v = max(rows, key=lambda r: (r[1]["cohen_kappa"], r[1]["agreement"]))
    print(f"\nBest agreement with humans: **{best_name}** (κ={best_v['cohen_kappa']})")
    print(
        "\nκ is the honest number here: agreement alone is inflated by the fact "
        "that most verdicts are 'correct', which a judge can match by guessing."
    )

    # Show where the best judge and the humans actually part ways — the
    # disagreements are the interesting artifact, not the summary statistic.
    if best_v["disagreements"]:
        print(f"\n### Where {best_name} disagrees with the humans\n")
        for d in best_v["disagreements"]:
            print(f"- human=`{d['human']}` judge=`{d['judge']}`")
            print(f"  - expected: {d['expected']}")
            print(f"  - predicted: {d['predicted']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
