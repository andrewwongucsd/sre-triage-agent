"""Core data structures shared across the agent and eval harness."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, TypedDict

# Sentinel used by the NO-DATA guardrail. When the tools surface no usable
# signal, the agent must emit this instead of guessing a team.
NO_DATA = "NO_DATA"


@dataclass
class ToolCall:
    """A single tool the model wants to invoke.

    ``id`` is the provider-assigned tool_use id when the backend supplies one
    (Anthropic does); the agent loop fills in a deterministic id otherwise.
    """

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    id: str | None = None


@dataclass
class ModelResponse:
    """One step of the agent loop: either the model wants tools, or it's done.

    Exactly one of ``tool_calls`` / ``final`` is populated.
    """

    tool_calls: list[ToolCall] = field(default_factory=list)
    final: str | None = None


@dataclass
class Diagnosis:
    """The agent's structured output — the thing we score."""

    root_cause: str
    evidence: list[str]
    escalate_to: str  # a team name, or NO_DATA
    confidence: float  # 0.0 - 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def no_data(reason: str = "insufficient signal from tools") -> "Diagnosis":
        return Diagnosis(
            root_cause=NO_DATA,
            evidence=[reason],
            escalate_to=NO_DATA,
            confidence=0.0,
        )


class AgentState(TypedDict, total=False):
    """LangGraph state threaded through the graph nodes."""

    scenario: str
    service: str
    window: str
    transcript: list[dict[str, Any]]  # neutral event log (see agent.py)
    pending_calls: list[dict[str, Any]]
    final_text: str | None
    iterations: int
    nudged: bool  # whether the "go gather signal first" nudge has already fired
    diagnosis: dict[str, Any]
