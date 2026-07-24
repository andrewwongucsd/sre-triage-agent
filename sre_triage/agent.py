"""The LangGraph agent: gather signal via tools, then synthesize a diagnosis.

Graph shape (a real ReAct loop with a guardrail on the way out):

    START -> agent -> (wants tools?) ---> tools -> agent
                    |-> (no signal yet) -> nudge -> agent
                    \\-> (done) ---------> finalize -> END

``agent``    asks the model for the next action (tool calls or a final answer).
``tools``    executes requested tools and appends results to the transcript.
``nudge``    sends the model back once if it tried to conclude without evidence.
``finalize`` parses the model's JSON and applies the NO-DATA guardrail.

The transcript is a neutral event log (list of dicts) so both the Anthropic and
mock backends can be driven by the same graph:

    {"role": "user", "text": ..., "service": ..., "window": ...}
    {"role": "assistant_tools", "tool_calls": [{"name", "args", "id"}, ...]}
    {"role": "tool", "name": ..., "args": ..., "id": ..., "result": ...}
    {"role": "assistant_text", "text": ...}
"""

from __future__ import annotations

import json
from typing import Any

from langgraph.graph import END, START, StateGraph

from .model import Backend, get_backend
from .schemas import NO_DATA, AgentState, Diagnosis
from .tools import TOOL_SPECS, TOOLS

MAX_ITERATIONS = 4  # guard against tool-call loops

# Tools that can actually surface incident signal. get_dependency_map is org
# structure, not evidence — reading it alone is not "looking at the incident".
SIGNAL_TOOLS = ("query_logs", "query_metrics")

NUDGE = (
    "You have not queried any signal yet. Call query_logs and query_metrics for this "
    "service before concluding — do not answer from the incident description alone."
)


def build_graph(backend: Backend):
    def agent_node(state: AgentState) -> dict[str, Any]:
        transcript = state["transcript"]
        resp = backend.step(transcript, TOOL_SPECS)
        if resp.tool_calls:
            # Every call carries an id so its result can be paired back to it. The
            # backend's own id wins; the transcript position is a unique fallback.
            n = len(transcript)
            calls = [
                {"name": c.name, "args": c.args, "id": c.id or f"call_{n}_{i}_{c.name}"}
                for i, c in enumerate(resp.tool_calls)
            ]
            return {
                "transcript": transcript + [{"role": "assistant_tools", "tool_calls": calls}],
                "pending_calls": calls,
                "final_text": None,
            }
        final = resp.final or ""
        # Record the answer so a follow-up turn (the nudge) has the full history.
        events = [{"role": "assistant_text", "text": final}] if final else []
        return {"transcript": transcript + events, "final_text": final, "pending_calls": []}

    def tools_node(state: AgentState) -> dict[str, Any]:
        new_events = []
        for call in state["pending_calls"]:
            fn = TOOLS.get(call["name"])
            result = fn(**call["args"]) if fn else {"error": f"unknown tool {call['name']}"}
            new_events.append(
                {
                    "role": "tool",
                    "name": call["name"],
                    "args": call["args"],
                    "id": call["id"],
                    "result": result,
                }
            )
        return {
            "transcript": state["transcript"] + new_events,
            "pending_calls": [],
            "iterations": state.get("iterations", 0) + 1,
        }

    def nudge_node(state: AgentState) -> dict[str, Any]:
        return {
            "transcript": state["transcript"] + [{"role": "user", "text": NUDGE}],
            "final_text": None,
            "nudged": True,
        }

    def finalize_node(state: AgentState) -> dict[str, Any]:
        diagnosis = _parse_and_guard(state["final_text"], state["transcript"])
        return {"diagnosis": diagnosis.to_dict()}

    def route(state: AgentState) -> str:
        if state.get("iterations", 0) >= MAX_ITERATIONS:
            return "finalize"  # give up looping; guardrail handles empties
        if state.get("pending_calls"):
            return "tools"
        # The model wants to conclude. If it never looked at logs or metrics, send it
        # back once: otherwise the NO-DATA guardrail fires on what is really an agent
        # failure, and a broken run scores as a correctly-cautious one.
        if not state.get("nudged") and not _queried_signal(state["transcript"]):
            return "nudge"
        return "finalize"

    g = StateGraph(AgentState)
    g.add_node("agent", agent_node)
    g.add_node("tools", tools_node)
    g.add_node("nudge", nudge_node)
    g.add_node("finalize", finalize_node)
    g.add_edge(START, "agent")
    g.add_conditional_edges(
        "agent", route, {"tools": "tools", "nudge": "nudge", "finalize": "finalize"}
    )
    g.add_edge("tools", "agent")
    g.add_edge("nudge", "agent")
    g.add_edge("finalize", END)
    return g.compile()


def _parse_and_guard(final_text: str | None, transcript: list[dict[str, Any]]) -> Diagnosis:
    # Guardrail (deterministic, independent of the model): if the tools never
    # surfaced signal, force NO_DATA regardless of what the model claimed. The two
    # empty-handed cases carry distinct reasons — "the window was quiet" is a
    # correct NO_DATA, "the agent never looked" is a bug wearing its costume.
    if not _queried_signal(transcript):
        return Diagnosis.no_data("agent concluded without querying logs or metrics")
    if not _has_signal(transcript):
        return Diagnosis.no_data("tools returned no logs and no metric anomaly")

    parsed = _extract_json(final_text)
    if parsed is None:
        return Diagnosis.no_data("model did not return parseable JSON")
    try:
        diag = Diagnosis(
            root_cause=str(parsed.get("root_cause", NO_DATA)),
            evidence=list(parsed.get("evidence", [])),
            escalate_to=str(parsed.get("escalate_to", NO_DATA)),
            confidence=float(parsed.get("confidence", 0.0)),
        )
    except (TypeError, ValueError):
        return Diagnosis.no_data("model returned malformed diagnosis fields")
    # If the model itself decided NO_DATA, normalize confidence to 0.
    if diag.escalate_to == NO_DATA:
        diag.confidence = 0.0
    return diag


def _queried_signal(transcript: list[dict[str, Any]]) -> bool:
    """True once the agent has actually run a signal-bearing tool."""
    return any(
        ev.get("role") == "tool" and ev["name"] in SIGNAL_TOOLS for ev in transcript
    )


def _has_signal(transcript: list[dict[str, Any]]) -> bool:
    for ev in transcript:
        if ev.get("role") != "tool":
            continue
        res = ev.get("result", {})
        if ev["name"] == "query_logs" and res.get("lines"):
            return True
        if ev["name"] == "query_metrics" and res.get("anomaly"):
            return True
    return False


def _extract_json(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def diagnose(scenario: str, service: str, window: str = "last_30m", backend: str | Backend = "anthropic") -> Diagnosis:
    """Run the agent on one incident and return its Diagnosis.

    ``backend`` may be a name ("anthropic"/"mock") or a Backend instance (handy
    for tests and for reusing one client across a whole eval run).
    """
    be = backend if not isinstance(backend, str) else get_backend(backend)
    graph = build_graph(be)
    initial: AgentState = {
        "scenario": scenario,
        "service": service,
        "window": window,
        "transcript": [
            {
                "role": "user",
                "text": f"Incident on service '{service}' (window {window}):\n{scenario}",
                "service": service,
                "window": window,
            }
        ],
        "pending_calls": [],
        "iterations": 0,
        "nudged": False,
        "final_text": None,
    }
    out = graph.invoke(initial)
    d = out["diagnosis"]
    return Diagnosis(**d)
