# sre-triage-agent

[![eval](https://github.com/andrewwongucsd/sre-triage-agent/actions/workflows/eval.yml/badge.svg)](https://github.com/andrewwongucsd/sre-triage-agent/actions/workflows/eval.yml)
![python](https://img.shields.io/badge/python-3.13-blue)
![agent](https://img.shields.io/badge/agent-LangGraph-orange)

> **An LLM incident-triage agent on LangGraph — with an evaluation harness that measures its own judge (Cohen's κ) and gates CI on quality.**

An LLM **incident-triage agent** built on **LangGraph** — it reads an incident,
calls tools (logs / metrics / dependency map), and returns a structured
diagnosis: likely root cause + which team to escalate to. Ships with a real
**evaluation harness**: a labeled benchmark, deterministic + LLM-as-judge
scoring, a validated judge, and a **CI regression gate** that fails the build if
quality drops.

> Origin story: I built a version of this on the raw API for an on-call rotation —
> dependency→team escalation with a circuit-breaker instinct. This is that idea
> rebuilt on a proper agent framework, with the evals I didn't have time for the
> first time.

## Why this exists

Two things separate "I've called an LLM API" from "I build agent systems":
using an **agent framework**, and being able to **measure agent quality**. This
repo is a small, honest demonstration of both. The eval harness is the point;
the agent is deliberately compact.

## Architecture

```
                 ┌──────────────── LangGraph StateGraph ────────────────┐
  incident  ───▶ │  agent ──(wants tools?)──▶ tools ──┐                 │ ──▶ Diagnosis
  (scenario,     │    ▲                                │                 │     { root_cause,
   service)      │    ├──(no signal yet)──▶ nudge ─────┘                 │       evidence,
                 │    └────────────────────────────────┘                 │       escalate_to,
                 │  agent ──(done)──▶ finalize ─(NO-DATA guardrail)─▶ END │       confidence }
                 └──────────────────────────────────────────────────────┘
```

- **Tools** (`sre_triage/tools.py`): `get_dependency_map`, `query_metrics`,
  `query_logs`. Synthetic data today; the signatures mirror real backends so the
  swap is mechanical.
- **Agent loop** (`sre_triage/agent.py`): a real ReAct loop — the model decides
  which tools to call, results feed back, then it emits structured JSON.
- **NO-DATA guardrail**: if the tools surface no logs *and* no metric anomaly,
  the agent emits `NO_DATA` instead of guessing a team. Enforced deterministically
  in `finalize` so it holds regardless of what the model says — the
  circuit-breaker instinct from real oncall work. A model that tries to conclude
  *without querying anything* is sent back once (the `nudge` node) rather than
  silently counted as a cautious `NO_DATA`; if it still refuses to look, the
  guardrail fires with a distinct reason so the failure is visible in the results.
- **Swappable model** (`sre_triage/model.py`): `--model anthropic` (Claude, the
  default) or `--model mock` (deterministic keyword baseline, no network — used
  by the tests and CI).

## Quickstart

```bash
make install                                   # venv + deps
make demo                                       # run the agent on one incident (mock backend)
make eval                                        # offline eval + regression gate (no API key)

export ANTHROPIC_API_KEY=sk-ant-...            # for the real model
python -m sre_triage --incident reco-featurestore-slow   # real Claude on a hard case
make eval-real                                   # full benchmark with Claude + Claude judge
```

## Eval methodology

The benchmark is 25 labeled incidents (`evals/cases.jsonl`, generated from
`sre_triage/fixtures.py`) across three difficulties: **clear** (one component
obviously at fault), **ambiguous** (logs and metrics implicate *different*
dependencies — the logs are a red herring), and **no_data** (correct answer is
`NO_DATA`).

Three layers of scoring, by design (`evals/run.py`):

1. **Headline — deterministic.** Exact match on `escalate_to`. Reproducible, no
   judge required. This is what the CI gate uses.
2. **Secondary — LLM-as-judge.** Claude grades `root_cause` quality
   (correct / partial / wrong) where determinism is impossible.
3. **Judge validation.** `validate_judge()` runs the judge against 15
   hand-labeled examples (`evals/human_labels.jsonl`) and reports
   agreement + Cohen's κ — so the judge is *measured*, not trusted.

### Results

Both columns are the same 25 cases and the same scorer. **Baseline** is the
offline keyword agent + heuristic judge (`make eval`, no API key, fully
reproducible). **Claude** is `claude-sonnet-5` as both agent and judge
(`make eval-real`).

| metric | baseline | Claude |
| --- | --- | --- |
| **escalation accuracy (headline)** | **88.0%** | **100.0%** |
| — clear (14 cases) | 100% | 100% |
| — ambiguous (6 cases) | 50% | 100% |
| — no_data (5 cases) | 100% | 100% |
| NO_DATA recall (guardrail) | 100% | 100% |
| false NO_DATA rate | 0% | 0% |
| root_cause quality (judge) | 25% | 100% |
| judge vs human agreement / κ | 80% / 0.64 | 80% / 0.656 |

The gap is entirely in the **ambiguous** band, which is the point of the split.
Those cases put the logs and the metrics in disagreement: a dependency named in
the error logs is a red herring, and the real culprit only shows up in the
metric series. The keyword baseline trusts the logs and gets 3 of 6 wrong by
construction. Claude reads past the red herring on all six — and its confidence
drops to 0.75–0.78 on exactly those, versus 0.85–0.9 on the unambiguous ones.

Two caveats worth stating plainly:

- **root_cause quality is Claude grading Claude.** That 100% comes from a judge
  that agrees with human labels only 80% of the time (κ=0.656) and can share the
  agent's blind spots. It is a secondary metric and not the gate, for this reason.
- **The benchmark is saturated at the top.** A frontier model scoring 100% can
  no longer rank *good* against *excellent* here. The CI gate at 0.85 still
  catches regressions, but the next real improvement to this repo is harder
  cases, not more of the same ones.

## CI regression gate

`.github/workflows/eval.yml` runs on every push/PR:

- **`eval-mock`** — offline, deterministic, no secret. Runs the tests and fails
  the build if escalation accuracy `< 0.8`. This is the build-blocking gate.
- **`eval-claude`** — runs the real benchmark against Claude, gated at `0.85`,
  **only if** the `ANTHROPIC_API_KEY` repo secret is set
  (*Settings → Secrets and variables → Actions*); otherwise it skips cleanly.

## Honest limitations

- **Synthetic data.** Tools read fixtures, not real logging/metrics backends.
  The benchmark measures triage *reasoning*, not data plumbing.
- **Small benchmark, and saturated.** 25 cases — enough to catch regressions,
  not enough for tight confidence intervals. Claude scores 100%, so the
  benchmark currently has no headroom to distinguish strong models from each
  other. Grow it (and make it harder) before trusting small score deltas.
- **Judge bias.** LLM-as-judge is imperfect (here: 80% human agreement, κ≈0.65)
  and can share the agent model's blind spots — on `make eval-real` the judge
  and the agent are the same model. Keep it secondary and spot-check.
- **Guardrail is coarse.** "No logs and no anomaly ⇒ NO_DATA" is deliberately
  simple; real triage has partial-signal cases this doesn't model yet.

## Layout

```
sre_triage/      agent (LangGraph), tools, model backends, fixtures, schemas
evals/           cases.jsonl, run.py (harness+gate), judge.py, human_labels.jsonl
scripts/         gen_cases.py (fixtures -> cases.jsonl)
tests/           pytest suite (runs on the mock backend, no API key)
.github/         CI regression gate
```
