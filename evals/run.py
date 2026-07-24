"""Eval harness + CI regression gate for the SRE triage agent.

    # offline: mock agent + heuristic judge (what CI runs)
    python evals/run.py --model mock --judge mock --validate-judge --gate 0.8

    # real: Claude agent + Claude judge
    python evals/run.py --model anthropic --judge anthropic --validate-judge

Metrics
  Headline (deterministic): escalation accuracy = exact match on escalate_to.
  Secondary (LLM judge):    root_cause quality = correct / partial / wrong.
  Guardrail:                no-data recall + false-NO_DATA rate.
  Judge quality:            agreement + Cohen's kappa vs human labels.

Exits non-zero if escalation accuracy < --gate (the regression gate).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evals.judge import get_judge, validate_judge  # noqa: E402
from sre_triage.agent import diagnose  # noqa: E402
from sre_triage.fixtures import by_id  # noqa: E402
from sre_triage.model import get_backend  # noqa: E402
from sre_triage.schemas import NO_DATA  # noqa: E402
from sre_triage.tools import set_active_incident  # noqa: E402


def load_cases(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def run_agent_over_cases(cases: list[dict], model: str) -> list[dict]:
    backend = get_backend(model)  # reuse one client across all cases
    rows = []
    for case in cases:
        set_active_incident(by_id(case["id"]))
        diag = diagnose(case["scenario"], case["service"], case["window"], backend=backend)
        rows.append(
            {
                "id": case["id"],
                "difficulty": case["difficulty"],
                "expected_escalate_to": case["expected_escalate_to"],
                "predicted_escalate_to": diag.escalate_to,
                "escalation_correct": diag.escalate_to == case["expected_escalate_to"],
                "expected_root_cause": case["expected_root_cause"],
                "predicted_root_cause": diag.root_cause,
                "confidence": diag.confidence,
            }
        )
    return rows


def score(rows: list[dict], judge_name: str) -> dict:
    total = len(rows)
    correct = sum(r["escalation_correct"] for r in rows)

    by_diff: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_diff[r["difficulty"]].append(r)
    diff_acc = {
        d: round(sum(x["escalation_correct"] for x in rs) / len(rs), 3)
        for d, rs in by_diff.items()
    }

    no_data_rows = [r for r in rows if r["expected_escalate_to"] == NO_DATA]
    signal_rows = [r for r in rows if r["expected_escalate_to"] != NO_DATA]
    no_data_recall = (
        round(sum(r["predicted_escalate_to"] == NO_DATA for r in no_data_rows) / len(no_data_rows), 3)
        if no_data_rows else None
    )
    false_no_data = (
        round(sum(r["predicted_escalate_to"] == NO_DATA for r in signal_rows) / len(signal_rows), 3)
        if signal_rows else None
    )

    result = {
        "escalation_accuracy": round(correct / total, 3) if total else 0.0,
        "escalation_accuracy_by_difficulty": diff_acc,
        "no_data_recall": no_data_recall,
        "false_no_data_rate": false_no_data,
        "n_cases": total,
    }

    # Secondary: LLM-as-judge on root_cause quality (signal cases only).
    if judge_name != "none":
        judge = get_judge(judge_name)
        verdicts = Counter()
        for r in signal_rows:
            v = judge.classify(r["predicted_root_cause"], r["expected_root_cause"])
            r["root_cause_verdict"] = v
            verdicts[v] += 1
        n = sum(verdicts.values()) or 1
        result["root_cause_quality"] = {
            "judge": judge_name,
            "correct": verdicts["correct"],
            "partial": verdicts["partial"],
            "wrong": verdicts["wrong"],
            "score": round((verdicts["correct"] + 0.5 * verdicts["partial"]) / n, 3),
        }
    return result


def render_markdown(result: dict, validation: dict | None) -> str:
    lines = ["## Eval results", ""]
    lines.append(f"**Escalation accuracy (headline): {result['escalation_accuracy']:.1%}** "
                 f"over {result['n_cases']} cases")
    lines.append("")
    lines.append("| metric | value |")
    lines.append("| --- | --- |")
    for d, acc in sorted(result["escalation_accuracy_by_difficulty"].items()):
        lines.append(f"| escalation accuracy — {d} | {acc:.1%} |")
    if result["no_data_recall"] is not None:
        lines.append(f"| NO_DATA recall (guardrail) | {result['no_data_recall']:.1%} |")
    if result["false_no_data_rate"] is not None:
        lines.append(f"| false NO_DATA rate | {result['false_no_data_rate']:.1%} |")
    rq = result.get("root_cause_quality")
    if rq:
        lines.append(f"| root_cause quality score (judge={rq['judge']}) | {rq['score']:.1%} |")
        lines.append(f"| root_cause verdicts | {rq['correct']} correct / "
                     f"{rq['partial']} partial / {rq['wrong']} wrong |")
    if validation:
        lines.append(f"| judge vs human agreement | {validation['agreement']:.1%} |")
        lines.append(f"| judge Cohen's kappa | {validation['cohen_kappa']} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="SRE triage agent eval harness")
    p.add_argument("--model", default="mock", choices=["anthropic", "mock"])
    p.add_argument("--judge", default="mock", choices=["anthropic", "mock", "none"])
    p.add_argument("--validate-judge", action="store_true",
                   help="measure the judge against evals/human_labels.jsonl")
    p.add_argument("--gate", type=float, default=None,
                   help="fail (exit 1) if escalation accuracy < this threshold")
    p.add_argument("--cases", default=str(ROOT / "evals" / "cases.jsonl"))
    p.add_argument("--out", default=None, help="path to write results JSON (default: evals/results/<ts>.json)")
    args = p.parse_args(argv)

    cases = load_cases(pathlib.Path(args.cases))
    validation = None
    try:
        rows = run_agent_over_cases(cases, args.model)
        result = score(rows, args.judge)
        if args.validate_judge and args.judge != "none":
            validation = validate_judge(get_judge(args.judge),
                                        str(ROOT / "evals" / "human_labels.jsonl"))
            result["judge_validation"] = {
                k: v for k, v in validation.items() if k != "disagreements"
            }
    except RuntimeError as e:  # e.g. a missing API key on the anthropic model/judge
        print(f"error: {e}", file=sys.stderr)
        return 2

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "run": ts,
        "model": args.model,
        "judge": args.judge,
        "summary": result,
        "cases": rows,
    }
    out_path = pathlib.Path(args.out) if args.out else ROOT / "evals" / "results" / f"{ts}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))

    try:
        shown = out_path.relative_to(ROOT)
    except ValueError:
        shown = out_path  # --out pointed outside the repo; print it as given

    md = render_markdown(result, validation)
    print(md)
    print(f"\nsaved: {shown}")
    if validation and validation["disagreements"]:
        print(f"\njudge disagreed with humans on {len(validation['disagreements'])}/{validation['n']} "
              f"labels (see JSON for details)")

    if args.gate is not None:
        acc = result["escalation_accuracy"]
        if acc < args.gate:
            print(f"\n❌ GATE FAILED: escalation accuracy {acc:.1%} < threshold {args.gate:.1%}")
            return 1
        print(f"\n✅ GATE PASSED: escalation accuracy {acc:.1%} >= threshold {args.gate:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
