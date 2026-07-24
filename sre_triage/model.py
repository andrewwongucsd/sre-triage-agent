"""Swappable model backends.

The agent loop (``agent.py``) is written against the ``Backend`` protocol so the
same LangGraph graph runs on real Claude or on a deterministic offline mock.

- ``AnthropicBackend`` — the real default. Uses Claude's native tool-use loop.
- ``MockBackend``     — deterministic, no network. A keyword-heuristic baseline
  used by the test suite and CI so the repo runs green without a secret. It is a
  *baseline*, not an oracle: it is designed to be wrong on genuinely ambiguous
  cases, which is what makes the eval scores meaningful.

Pick one with ``get_backend("anthropic" | "mock")``.
"""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

from .schemas import ModelResponse, ToolCall

SYSTEM_PROMPT = """You are an SRE incident-triage assistant. Given an incident \
for a specific service, use the tools to gather signal, then produce a single \
structured diagnosis.

Rules:
- Always inspect the dependency map, metrics, and logs before concluding.
- Escalate to the team that OWNS the true root-cause dependency, not necessarily \
the team that owns the failing service. A dependency named in error logs may be a \
red herring if the metrics implicate a different one.
- Before escalating to a dependency, query THAT dependency's own logs and metrics. \
A degraded dependency is often itself blocked on something further upstream. \
Escalate to the root cause, not to the first component that looks broken.
- If the tools surface no usable signal (empty logs AND no metric anomaly), you \
MUST return escalate_to="NO_DATA" and root_cause="NO_DATA". Never guess a team \
without evidence.

When done, respond with ONLY a JSON object (no prose) of the form:
{"root_cause": str, "evidence": [str, ...], "escalate_to": str, "confidence": float}
confidence is 0.0-1.0. escalate_to is a team name or "NO_DATA"."""


class Backend(Protocol):
    def step(self, transcript: list[dict[str, Any]], tool_specs: list[dict]) -> ModelResponse:
        """Advance the agent one turn given the neutral transcript so far."""
        ...


# --------------------------------------------------------------------------- #
# Anthropic (real default)
# --------------------------------------------------------------------------- #
class AnthropicBackend:
    def __init__(self, model: str = "claude-sonnet-5", max_tokens: int = 1024) -> None:
        # Imported lazily so `--model mock` works with anthropic uninstalled.
        import anthropic

        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set — export it to use --model anthropic, "
                "or run with --model mock (offline, no key needed)."
            )
        self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        self._model = model
        self._max_tokens = max_tokens

    def step(self, transcript: list[dict[str, Any]], tool_specs: list[dict]) -> ModelResponse:
        messages = _transcript_to_anthropic(transcript)
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=SYSTEM_PROMPT,
            tools=tool_specs,
            messages=messages,
        )
        tool_calls: list[ToolCall] = []
        text_parts: list[str] = []
        for block in resp.content:
            if block.type == "tool_use":
                # Carry Anthropic's tool_use id through the transcript so the
                # matching tool_result can be paired with it later.
                tool_calls.append(ToolCall(name=block.name, args=dict(block.input), id=block.id))
            elif block.type == "text":
                text_parts.append(block.text)
        if tool_calls:
            return ModelResponse(tool_calls=tool_calls)
        return ModelResponse(final="".join(text_parts).strip())


def _transcript_to_anthropic(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render the neutral transcript as Anthropic messages.

    tool_use / tool_result blocks are paired by the id carried on each event, and
    all results for one assistant turn are collected into a *single* user message
    — splitting them across messages trains the model out of parallel tool calls.
    """
    messages: list[dict[str, Any]] = []
    for ev in transcript:
        role = ev["role"]
        if role == "user":
            messages.append({"role": "user", "content": ev["text"]})
        elif role == "assistant_text":
            messages.append({"role": "assistant", "content": ev["text"]})
        elif role == "assistant_tools":
            messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": call["id"],
                            "name": call["name"],
                            "input": call["args"],
                        }
                        for call in ev["tool_calls"]
                    ],
                }
            )
        elif role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": ev["id"],
                "content": json.dumps(ev["result"]),
            }
            prev = messages[-1] if messages else None
            if prev and prev["role"] == "user" and isinstance(prev["content"], list):
                prev["content"].append(block)
            else:
                messages.append({"role": "user", "content": [block]})
    return messages


# --------------------------------------------------------------------------- #
# Mock (deterministic, offline)
# --------------------------------------------------------------------------- #
class MockBackend:
    """A keyword-heuristic baseline.

    Turn 1: request all three tools. Turn 2 (once results are present): apply a
    deterministic heuristic to synthesize a diagnosis. Intentionally beatable on
    ambiguous incidents where the logs implicate a different dependency than the
    metrics do.
    """

    def step(self, transcript: list[dict[str, Any]], tool_specs: list[dict]) -> ModelResponse:
        results = {ev["name"]: ev["result"] for ev in transcript if ev["role"] == "tool"}
        if not results:
            svc = _first_service(transcript)
            return ModelResponse(
                tool_calls=[
                    ToolCall("get_dependency_map", {"service": svc}),
                    ToolCall("query_metrics", {"service": svc, "window": "last_30m"}),
                    ToolCall("query_logs", {"service": svc, "window": "last_30m"}),
                ]
            )
        return ModelResponse(final=json.dumps(_heuristic_diagnosis(results)))


def _first_service(transcript: list[dict[str, Any]]) -> str:
    for ev in transcript:
        if ev["role"] == "user":
            return ev.get("service", "unknown-service")
    return "unknown-service"


def _heuristic_diagnosis(results: dict[str, Any]) -> dict[str, Any]:
    dep_map = results.get("get_dependency_map", {})
    metrics = results.get("query_metrics", {})
    logs = results.get("query_logs", {})

    lines = logs.get("lines", [])
    error_terms = [t.lower() for t in logs.get("error_terms", [])]
    anomaly = bool(metrics.get("anomaly"))

    # NO-DATA: no logs and no metric anomaly -> refuse to guess.
    if not lines and not anomaly:
        return {
            "root_cause": "NO_DATA",
            "evidence": ["logs empty and no metric anomaly in window"],
            "escalate_to": "NO_DATA",
            "confidence": 0.0,
        }

    upstream = dep_map.get("upstream", [])
    implicated_metric = [d.lower() for d in metrics.get("implicated", [])]

    # Heuristic precedence: a dependency named in the ERROR LOGS wins. This is
    # the deliberate weakness — when logs point at a red herring, the baseline
    # follows the logs and gets ambiguous cases wrong.
    chosen = None
    for dep in upstream:
        if dep["name"].lower() in error_terms:
            chosen = dep
            break
    if chosen is None:
        for dep in upstream:
            if dep["name"].lower() in implicated_metric:
                chosen = dep
                break

    top_error = lines[0] if lines else metrics.get("summary", "anomaly detected")
    svc = dep_map.get("service", "service")
    if chosen is not None:
        symptom = ", ".join(t for t in logs.get("error_terms", []) if t.lower() != chosen["name"].lower())
        escalate_to = chosen["team"]
        root_cause = f"{chosen['name']} {symptom} degrading {svc}".replace("  ", " ")
    else:
        symptom = ", ".join(logs.get("error_terms", [])) or "unhandled error"
        escalate_to = dep_map.get("owning_team", "NO_DATA")
        root_cause = f"{svc} internal fault ({symptom}) with no clear upstream cause"

    return {
        "root_cause": root_cause,
        "evidence": [top_error, metrics.get("summary", "")][: 2 if metrics.get("summary") else 1],
        "escalate_to": escalate_to,
        "confidence": 0.8 if (lines and anomaly) else 0.55,
    }


def get_backend(name: str) -> Backend:
    name = (name or "anthropic").lower()
    if name == "mock":
        return MockBackend()
    if name == "anthropic":
        return AnthropicBackend(model=os.environ.get("SRE_MODEL", "claude-sonnet-5"))
    raise ValueError(f"unknown backend: {name!r} (expected 'anthropic' or 'mock')")
