"""Tests run entirely on the deterministic mock backend — no API key needed."""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import evals.run as evals_run  # noqa: E402
from evals.judge import MockJudge, cohen_kappa, validate_judge  # noqa: E402
from evals.run import load_cases, run_agent_over_cases, score  # noqa: E402
from sre_triage.agent import diagnose  # noqa: E402
from sre_triage.fixtures import INCIDENTS, by_id  # noqa: E402
from sre_triage.model import MockBackend, _transcript_to_anthropic  # noqa: E402
from sre_triage.schemas import NO_DATA, ModelResponse, ToolCall  # noqa: E402
from sre_triage.tools import set_active_incident  # noqa: E402

CASES = str(ROOT / "evals" / "cases.jsonl")


def _run(incident_id: str):
    inc = by_id(incident_id)
    set_active_incident(inc)
    return diagnose(inc["scenario"], inc["service"], inc["window"], backend="mock")


def test_clear_case_escalates_correctly():
    d = _run("checkout-db-pool")
    assert d.escalate_to == "database-platform"
    assert d.confidence > 0.5


def test_no_data_guardrail_fires():
    d = _run("quiet-slowness")
    assert d.escalate_to == NO_DATA
    assert d.root_cause == NO_DATA
    assert d.confidence == 0.0


def test_ambiguous_case_is_a_known_miss_for_baseline():
    # The keyword baseline follows the red-herring logs and blames the cache.
    # This test *documents* the gap the real model is expected to close.
    d = _run("webhook-outbox-locks")
    assert d.escalate_to == "cache-redis"
    assert by_id("webhook-outbox-locks")["label"]["expected_escalate_to"] == "database-platform"


def test_every_incident_produces_valid_diagnosis():
    for inc in INCIDENTS:
        set_active_incident(inc)
        d = diagnose(inc["scenario"], inc["service"], inc["window"], backend="mock")
        assert 0.0 <= d.confidence <= 1.0
        assert isinstance(d.evidence, list)
        assert d.escalate_to  # non-empty


def test_eval_gate_passes_on_mock_baseline():
    rows = run_agent_over_cases(load_cases(pathlib.Path(CASES)), "mock")
    summary = score(rows, judge_name="none")
    assert summary["escalation_accuracy"] >= 0.8  # the CI gate threshold
    assert summary["no_data_recall"] == 1.0
    assert summary["false_no_data_rate"] == 0.0


def test_clear_cases_are_all_correct():
    rows = run_agent_over_cases(load_cases(pathlib.Path(CASES)), "mock")
    assert score(rows, "none")["escalation_accuracy_by_difficulty"]["clear"] == 1.0


def test_judge_validation_reports_agreement_and_kappa():
    v = validate_judge(MockJudge(), str(ROOT / "evals" / "human_labels.jsonl"))
    assert v["n"] == 15
    assert 0.0 <= v["agreement"] <= 1.0
    assert -1.0 <= v["cohen_kappa"] <= 1.0


def test_cohen_kappa_perfect_and_chance():
    assert cohen_kappa(["correct", "wrong"], ["correct", "wrong"]) == 1.0
    # identical single-category labelings -> undefined-ish; our impl returns 1.0
    assert cohen_kappa(["correct", "correct"], ["correct", "correct"]) == 1.0


# --------------------------------------------------------------------------- #
# Anthropic message rendering (the --model anthropic path CI never exercises)
# --------------------------------------------------------------------------- #
_DIAGNOSIS_JSON = json.dumps(
    {
        "root_cause": "postgres-primary pool exhaustion",
        "evidence": ["pool timeout"],
        "escalate_to": "database-platform",
        "confidence": 0.9,
    }
)


def _anthropic_messages(incident_id: str, backend) -> list[dict]:
    """Run the graph on a backend, then render its transcript for the API."""
    from sre_triage.agent import build_graph

    inc = by_id(incident_id)
    set_active_incident(inc)
    graph = build_graph(backend)
    out = graph.invoke(
        {
            "scenario": inc["scenario"],
            "service": inc["service"],
            "window": inc["window"],
            "transcript": [
                {"role": "user", "text": inc["scenario"], "service": inc["service"]}
            ],
            "pending_calls": [],
            "iterations": 0,
            "nudged": False,
            "final_text": None,
        }
    )
    return _transcript_to_anthropic(out["transcript"])


def test_repeated_identical_tool_calls_get_distinct_ids():
    """Duplicate (name, args) pairs must not collide — the API rejects dupe ids."""

    class RepeatBackend:
        def __init__(self):
            self.turns = 0

        def step(self, transcript, tool_specs):
            self.turns += 1
            if self.turns <= 2:  # ask for the exact same call twice in a row
                return ModelResponse(
                    tool_calls=[ToolCall("query_logs", {"service": "checkout-api"})]
                )
            return ModelResponse(final=_DIAGNOSIS_JSON)

    messages = _anthropic_messages("checkout-db-pool", RepeatBackend())
    ids = [
        block["id"]
        for m in messages
        if isinstance(m["content"], list)
        for block in m["content"]
        if block.get("type") == "tool_use"
    ]
    assert len(ids) == 2
    assert len(set(ids)) == 2, f"duplicate tool_use ids: {ids}"


def test_parallel_tool_results_share_one_user_message():
    """All results for one assistant turn go back in a single user message."""
    messages = _anthropic_messages("checkout-db-pool", MockBackend())

    tool_use_msg = next(
        m for m in messages
        if m["role"] == "assistant"
        and isinstance(m["content"], list)
        and m["content"][0]["type"] == "tool_use"
    )
    assert len(tool_use_msg["content"]) == 3  # mock asks for all three tools at once

    results = [
        m for m in messages
        if m["role"] == "user"
        and isinstance(m["content"], list)
        and m["content"][0]["type"] == "tool_result"
    ]
    assert len(results) == 1, "tool results were split across messages"
    assert len(results[0]["content"]) == 3

    # ...and every result pairs with a tool_use id from that turn.
    assert {b["tool_use_id"] for b in results[0]["content"]} == {
        b["id"] for b in tool_use_msg["content"]
    }


# --------------------------------------------------------------------------- #
# The nudge: concluding without evidence is an agent failure, not a NO_DATA case
# --------------------------------------------------------------------------- #
class _NeverCallsTools:
    """Answers from the scenario alone, ignoring the nudge."""

    def __init__(self):
        self.turns = 0

    def step(self, transcript, tool_specs):
        self.turns += 1
        return ModelResponse(final=_DIAGNOSIS_JSON)


def test_conclusion_without_tools_is_nudged_once_then_flagged():
    backend = _NeverCallsTools()
    inc = by_id("checkout-db-pool")
    set_active_incident(inc)
    d = diagnose(inc["scenario"], inc["service"], inc["window"], backend=backend)

    assert backend.turns == 2, "expected exactly one nudge, then finalize"
    assert d.escalate_to == NO_DATA  # fail-safe: never escalate without evidence
    # The reason distinguishes this from a genuinely quiet window.
    assert "without querying" in d.evidence[0]


def test_nudge_recovers_a_model_that_forgot_to_gather():
    """After the nudge the model queries, and its real answer is used."""

    class ForgetsThenGathers:
        def step(self, transcript, tool_specs):
            if any(ev["role"] == "tool" for ev in transcript):
                return ModelResponse(final=_DIAGNOSIS_JSON)
            if any(ev["role"] == "user" and "query_logs" in ev["text"] for ev in transcript):
                return ModelResponse(
                    tool_calls=[
                        ToolCall("query_logs", {"service": "checkout-api"}),
                        ToolCall("query_metrics", {"service": "checkout-api"}),
                    ]
                )
            return ModelResponse(final=_DIAGNOSIS_JSON)

    inc = by_id("checkout-db-pool")
    set_active_incident(inc)
    d = diagnose(inc["scenario"], inc["service"], inc["window"], backend=ForgetsThenGathers())
    assert d.escalate_to == "database-platform"


def test_quiet_window_and_missing_tools_give_different_reasons():
    quiet = _run("quiet-slowness")  # mock queries; the window really is empty
    assert "no logs and no metric anomaly" in quiet.evidence[0]


# --------------------------------------------------------------------------- #
# CI gate plumbing
# --------------------------------------------------------------------------- #
def test_gate_still_reported_when_out_path_is_outside_the_repo(tmp_path, capsys):
    out = tmp_path / "results.json"
    code = evals_run.main(
        ["--model", "mock", "--judge", "none", "--gate", "0.95", "--out", str(out)]
    )
    captured = capsys.readouterr().out
    assert "GATE FAILED" in captured  # the verdict is computed, not skipped
    assert code == 1
    assert out.exists()


def test_gate_passes_with_out_path_outside_the_repo(tmp_path, capsys):
    out = tmp_path / "results.json"
    code = evals_run.main(
        ["--model", "mock", "--judge", "none", "--gate", "0.8", "--out", str(out)]
    )
    assert "GATE PASSED" in capsys.readouterr().out
    assert code == 0
