"""The three tools the agent can call.

Data is synthetic — each tool reads the "active incident" fixture rather than
real infra. In production these would hit your logging/metrics/service-catalog
backends; the signatures (service, window) are kept realistic so the swap is
mechanical.
"""

from __future__ import annotations

from typing import Any

# The runner sets the active incident before invoking the agent, so the tools
# know which synthetic incident's signal to return.
_ACTIVE: dict[str, Any] | None = None


def set_active_incident(incident: dict[str, Any]) -> None:
    global _ACTIVE
    _ACTIVE = incident


def _signal() -> dict[str, Any]:
    if _ACTIVE is None:
        raise RuntimeError("no active incident set — call set_active_incident() first")
    return _ACTIVE["signal"]


def query_logs(service: str, window: str = "last_30m") -> dict[str, Any]:
    """Return log snippets for a service over a time window."""
    logs = _signal()["logs"]
    return {
        "service": service,
        "window": window,
        "lines": logs["lines"],
        "error_terms": logs["error_terms"],
    }


def query_metrics(service: str, window: str = "last_30m") -> dict[str, Any]:
    """Return a metric summary (error rate, latency) for a service."""
    metrics = _signal()["metrics"]
    return {"service": service, "window": window, **metrics}


def get_dependency_map(service: str) -> dict[str, Any]:
    """Return the owning team + upstream dependencies for a service."""
    return dict(_signal()["dependency_map"])


TOOLS = {
    "query_logs": query_logs,
    "query_metrics": query_metrics,
    "get_dependency_map": get_dependency_map,
}

# Anthropic tool schemas (also serves as documentation of each tool's contract).
TOOL_SPECS = [
    {
        "name": "get_dependency_map",
        "description": "Owning team and upstream dependencies (with their teams) for a service.",
        "input_schema": {
            "type": "object",
            "properties": {"service": {"type": "string"}},
            "required": ["service"],
        },
    },
    {
        "name": "query_metrics",
        "description": "Error-rate and latency summary for a service over a window, with any implicated dependencies.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "window": {"type": "string", "description": "e.g. last_30m"},
            },
            "required": ["service"],
        },
    },
    {
        "name": "query_logs",
        "description": "Recent error log lines and extracted error terms for a service over a window.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "window": {"type": "string"},
            },
            "required": ["service"],
        },
    },
]
