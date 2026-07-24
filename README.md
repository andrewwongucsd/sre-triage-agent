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

The benchmark is 43 labeled incidents (`evals/cases.jsonl`, generated from
`sre_triage/fixtures.py`) across six bands, ordered roughly by what they demand
of the agent:

| band | n | what it tests |
| --- | --- | --- |
| `clear` | 14 | one component is unambiguously at fault |
| `ambiguous` | 6 | logs and metrics implicate *different* dependencies — the logs are a red herring |
| `no_data` | 5 | no logs and no anomaly; the correct answer is `NO_DATA` |
| `cascading` | 6 | the implicated dependency is itself a *victim*; the true root is visible only in that dependency's own telemetry, so the agent must query upstream |
| `partial_signal` | 6 | logs exist but describe no incident (client 4xx, deploy-drain noise, a retry-flap, one 500 in 31k). The guardrail does not fire, so the model itself must decline to name a team |
| `conflicting` | 6 | the obvious suspects are exonerated on inspection — the fault is the service's own release, or the evidence genuinely cannot separate two candidates |

The last three bands exist because the first three stopped discriminating: Claude
scored 100% on them, so the benchmark could catch a regression but could no
longer tell a good model from an excellent one. They started at 3 cases each and
were widened to 6 once it was clear one case swinging a band by 33 points made
the per-band numbers noise — see the caveat below, they are still small.

Three layers of scoring, by design (`evals/run.py`):

1. **Headline — deterministic.** Exact match on `escalate_to`. Reproducible, no
   judge required. This is what the CI gate uses.
2. **Secondary — LLM-as-judge.** Claude grades `root_cause` quality
   (correct / partial / wrong) where determinism is impossible.
3. **Judge validation.** `validate_judge()` runs the judge against 15
   hand-labeled examples (`evals/human_labels.jsonl`) and reports
   agreement + Cohen's κ — so the judge is *measured*, not trusted. A second,
   30-example reference set (`evals/reference_labels.jsonl`) provides a
   cross-model check — see below.

### Two reference sets, honestly labeled

Judge quality is measured against two references that answer different questions:

| set | n | graded by | what agreement with it means |
| --- | --- | --- | --- |
| `human_labels.jsonl` | 15 | a human | the number that matters — does the judge track human judgment? |
| `reference_labels.jsonl` | 30 | `claude-opus-4-8` | a cross-model check — how model-specific is the judge's grading? |

The reference set is **model-graded, and says so**: every row carries a
`verdict_source` naming the grading model, and the field is `verdict`, never
`human_verdict`. It is *not* a substitute for human labels and is never counted
as human agreement — doing so would make "I validated my judge against humans" a
false claim. Its value is orthogonal: the judge is `claude-sonnet-5`, the
reference grader is `claude-opus-4-8`, so their agreement bounds how much the
judge's verdicts are an artifact of one model versus a robust reading of the
rubric. (When `claude-opus-4-8` is itself run as a candidate judge against this
set, that row is self-agreement, not an independent check — read the
`claude-sonnet-5` and `claude-haiku-4-5` rows for cross-model signal.)

An early result already earns its keep: the crude keyword judge agrees with the
human labels 80% but with the 30-example Opus reference only **43%** — the
curated 15 were too easy to expose it, a broader set of the agent's actual
outputs was not. Grow both sets before trusting small deltas (`make
compare-judges` / `make compare-judges-ref`).

### Choosing the judge

`make eval-real` runs the same model as agent *and* judge, which is the standard
objection to LLM-as-judge: shared blind spots. Rather than assume an independent
judge fixes that, `make compare-judges` measures each candidate against the same
human labels:

| judge | agreement | Cohen's κ | disagreements |
| --- | --- | --- | --- |
| **claude-sonnet-5** (default) | 80.0% | **0.656** | 3/15 |
| mock (keyword heuristic) | 80.0% | 0.640 | 3/15 |
| claude-opus-4-8 | 73.3% | 0.577 | 4/15 |
| claude-haiku-4-5 | 60.0% | 0.323 | 6/15 |

Two things worth stating plainly, because the result is not what you'd guess:

- **The more capable model was not the better judge.** `claude-opus-4-8` agreed
  with the humans *less* than `claude-sonnet-5`, and less than the offline
  keyword heuristic. The default stays `claude-sonnet-5` — now on evidence
  rather than inertia.
- **This comparison is underpowered, and that is the more useful finding.** At
  n=15 a single flipped verdict moves κ by ≈0.08 — the entire gap between the
  top three rows. Only `claude-haiku-4-5` (6/15) is distinguishable from the
  rest. So the next real investment in this harness is **more human labels**,
  not a different judge model. Treat the ordering above as "haiku is worse, the
  others are a tie" and nothing finer.

### Results

Both columns are the same 43 cases and the same scorer. **Baseline** is the
offline keyword agent + heuristic judge (`make eval`, no API key, fully
reproducible). **Claude** is `claude-sonnet-5` as both agent and judge
(`make eval-real`).

| metric | baseline | Claude |
| --- | --- | --- |
| **escalation accuracy (headline)** | **53.5%** | **79.1%** |
| — clear (14) | 100% | 100% |
| — ambiguous (6) | 50% | 100% |
| — no_data (5) | 100% | 100% |
| — cascading (6) | 0% | 17% |
| — partial_signal (6) | 0% | 67% |
| — conflicting (6) | 17% | 67% |
| NO_DATA recall (guardrail) | 46.2% | 69.2% |
| false NO_DATA rate | 0% | 0% |
| root_cause quality (judge) | ~24% | 88% |
| judge vs human agreement / κ | 80% / 0.64 | 80–87% / 0.65–0.77 |

Read the shape, not just the headline. On the first three bands Claude is
perfect and the interesting split is **ambiguous**, where the logs and metrics
disagree: the baseline trusts the logs and gets 3 of 6 wrong by construction,
while Claude reads past the red herring on all six.

The last three bands are where it earns its keep — Claude misses 9 of 43, all in
those three, and each miss points at a different weakness:

- **cascading (17%)** — the hardest band, and the sharpest finding. The agent
  stops at the first genuinely-degraded dependency and escalates there, rather
  than querying that dependency and discovering it is itself a victim. This is a
  *procedural* gap, not a reasoning one. An earlier experiment confirmed a prompt
  nudge moves this band, but at the cost of over-investigating clear cases, for a
  flat headline (see the git history: `EXPERIMENT` → `Revert`). Worth revisiting;
  not yet won.
- **partial_signal (67%) / NO_DATA recall (69.2%)** — the same finding from two
  sides: the agent sometimes reads present-but-benign signal as an incident and
  names a team where it should refuse. Critically, **`false NO_DATA` is 0% in
  both columns** — the failure is one-directional. It never refuses when it
  should answer, only the reverse. For a triage tool that is the safe direction
  to be wrong in.
- **conflicting (67%)** — mostly handled; it does exonerate healthy dependencies
  when it inspects them, and correctly returns `NO_DATA` on the genuinely
  undecidable case.

Two caveats worth stating plainly:

- **root_cause quality is Claude grading Claude, and it moves.** Across six runs
  the deterministic headline metric reproduced exactly for a given benchmark,
  while the judged root_cause score wandered (100% → 95% → 95% → 97.5% → 90% →
  88%) — and the judge's own κ against the *fixed* 15 human labels swung from
  0.651 to **0.771** with nothing changed but the sampling. A metric that moves
  on its own while the reproducible one holds is the whole argument for gating CI
  on the deterministic number.
- **Still a small benchmark.** 43 cases; the three hard bands are 6 each, so one
  case is ~17 points. Widening them from 3 to 6 already moved cascading 33% → 17%
  and partial_signal 33% → 67% — proof the 3-case numbers had been noise. Treat
  the per-band numbers as directional and the headline as the measurement, and
  grow the bands further before trusting a small delta.

## CI regression gate

`.github/workflows/eval.yml` runs on every push/PR:

- **`eval-mock`** — offline, deterministic, no secret. Runs the tests and fails
  the build if escalation accuracy `< 0.50`. This is the build-blocking gate.
- **`eval-claude`** — runs the real benchmark against Claude, gated at `0.75`,
  **only if** the `ANTHROPIC_API_KEY` repo secret is set
  (*Settings → Secrets and variables → Actions*); otherwise it skips cleanly.
- **`compare-judges`** — opt-in via *Run workflow*; measures candidate judge
  models against the human labels (see [Choosing the judge](#choosing-the-judge)).

Both gates are **ratchets**, set just under the measured score (baseline 53.5%,
Claude 79.1%) so they detect regression rather than encode an aspiration. When a
change moves the score, the gate moves with it — deliberately, in the same
commit, with the new number in the message. (This is not hypothetical: widening
the benchmark dropped Claude 85.3% → 79.1%, tripped the old 0.80 gate, and the
gate was lowered to 0.75 in the same change — exactly the intended workflow.)

## Honest limitations

- **Synthetic data.** Tools read fixtures, not real logging/metrics backends.
  The benchmark measures triage *reasoning*, not data plumbing.
- **Small benchmark.** 43 cases — enough to catch regressions, not enough for
  tight confidence intervals, and the three hardest bands are 6 cases each (one
  case ≈ 17 points). It is not saturated (Claude 79.1%), so it can rank models
  rather than just detect regressions, but per-band numbers should be read as
  directional until the bands are grown further.
- **Judge bias, and too few labels to resolve it.** LLM-as-judge is imperfect
  (here: 80% human agreement, κ≈0.65) and on `make eval-real` the judge and the
  agent are the same model, so they can share blind spots. Swapping in an
  independent judge did *not* measurably help (see Choosing the judge), but with
  only 15 human labels the comparison cannot resolve differences smaller than one
  verdict. Growing `human_labels.jsonl` is the prerequisite for answering this
  properly.
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
