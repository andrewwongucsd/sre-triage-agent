"""LLM-as-judge for root_cause quality, plus a validation harness for the judge.

Two backends, same interface (``classify(predicted, expected) -> verdict``):
  - ``AnthropicJudge`` — the real judge. Claude grades against a rubric.
  - ``MockJudge``      — a deterministic token-overlap heuristic so the eval
                         pipeline (and CI) runs offline. Documented as a weak
                         stand-in, not the real grader.

`validate_judge()` is the senior move the plan calls out: run the judge against
hand-labeled examples and report agreement + Cohen's kappa, so we can *measure*
the judge rather than trust it.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Protocol

VERDICTS = ("correct", "partial", "wrong")

_JUDGE_SYSTEM = """You grade an SRE agent's proposed root_cause against a \
reference root_cause for the same incident. Reply with ONLY one word:
- "correct": same underlying cause and correct component/dependency.
- "partial": right area but wrong/missing specific component, or vague.
- "wrong": different cause, or blames the wrong component.
Judge the CAUSE, not the wording."""


class Judge(Protocol):
    def classify(self, predicted: str, expected: str) -> str: ...


# --------------------------------------------------------------------------- #
_STOP = {
    "the", "a", "an", "in", "on", "of", "and", "to", "with", "causing", "caused",
    "degradation", "failure", "error", "errors", "implicated", "issue", "service",
    "is", "are", "by", "for", "from", "at", "after", "into",
}


def _stem(w: str) -> str:
    # Light suffix stripping so evicting/evicted/evicts collapse together —
    # keeps the keyword judge from being defeated by verb tense. Still a blunt
    # instrument; that's the point (see validate_judge).
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9\-]+", text.lower())
    return {_stem(w) for w in words if w not in _STOP and len(w) > 2}


class MockJudge:
    """Deterministic keyword-overlap judge. Offline stand-in for the LLM judge."""

    def classify(self, predicted: str, expected: str) -> str:
        p, e = predicted.strip(), expected.strip()
        if e == "NO_DATA" or p == "NO_DATA":
            return "correct" if p == e else "wrong"
        pt, et = _tokens(p), _tokens(e)
        if not et:
            return "partial"
        overlap = len(pt & et) / len(et)
        if overlap >= 0.5:
            return "correct"
        if overlap >= 0.2:
            return "partial"
        return "wrong"


class AnthropicJudge:
    def __init__(self, model: str = "claude-sonnet-5") -> None:
        import anthropic

        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set — export it to use --judge anthropic, "
                "or run with --judge mock (offline, no key needed)."
            )
        self._client = anthropic.Anthropic()
        self._model = model

    def classify(self, predicted: str, expected: str) -> str:
        if expected == "NO_DATA" or predicted == "NO_DATA":
            return "correct" if predicted == expected else "wrong"
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=8,
            system=_JUDGE_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": f"reference: {expected}\nproposed: {predicted}\nverdict:",
                }
            ],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip().lower()
        for v in VERDICTS:
            if v in text:
                return v
        return "wrong"


def get_judge(name: str) -> Judge:
    name = (name or "mock").lower()
    if name == "mock":
        return MockJudge()
    if name == "anthropic":
        return AnthropicJudge(model=os.environ.get("SRE_JUDGE_MODEL", "claude-sonnet-5"))
    raise ValueError(f"unknown judge: {name!r}")


# --------------------------------------------------------------------------- #
# Judge validation: measure the judge against human labels.
# --------------------------------------------------------------------------- #
def cohen_kappa(a: list[str], b: list[str]) -> float:
    """Cohen's kappa between two labelings over VERDICTS categories."""
    n = len(a)
    if n == 0:
        return 0.0
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    pe = 0.0
    for cat in VERDICTS:
        pa = sum(1 for x in a if x == cat) / n
        pb = sum(1 for x in b if x == cat) / n
        pe += pa * pb
    if pe == 1.0:
        return 1.0
    return (po - pe) / (1 - pe)


def validate_judge(judge: Judge, labels_path: str) -> dict[str, Any]:
    """Run ``judge`` over hand-labeled (predicted, expected, human_verdict) rows."""
    rows = [json.loads(line) for line in open(labels_path) if line.strip()]
    human = [r["human_verdict"] for r in rows]
    machine = [judge.classify(r["predicted_root_cause"], r["expected_root_cause"]) for r in rows]
    agree = sum(1 for h, m in zip(human, machine) if h == m)
    disagreements = [
        {"predicted": r["predicted_root_cause"], "expected": r["expected_root_cause"],
         "human": h, "judge": m}
        for r, h, m in zip(rows, human, machine) if h != m
    ]
    return {
        "n": len(rows),
        "agreement": agree / len(rows) if rows else 0.0,
        "cohen_kappa": round(cohen_kappa(human, machine), 3),
        "disagreements": disagreements,
    }
