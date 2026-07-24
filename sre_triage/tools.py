"""The three tools the agent can call.

Data is synthetic — each tool reads the "active incident" fixture rather than
real infra. In production these would hit your logging/metrics/service-catalog
backends; the signatures (service, window) are kept realistic so the swap is
mechanical.

Telemetry is keyed by service. The incident's own service always has signal;
a dependency has signal only if the fixture defines it (see ``per_service`` in
``fixtures.py``). Querying anything else returns an explicit empty result rather
than the incident service's data under a different name — a synthetic backend
that answers every question with the same rows would quietly teach the agent
that its ``service`` argument does not matter.
"""

from __future__ import annotations

from typing import Any

# The runner sets the active incident before invoking the agent, so the tools
# know which synthetic incident's signal to return.
_ACTIVE: dict[str, Any] | None = None

_NO_LOGS: dict[str, Any] = {"lines": [], "error_terms": []}
_NO_METRICS: dict[str, Any] = {
    "anomaly": False,
    "summary": "no telemetry for this service in the window",
    "implicated": [],
}


def set_active_incident(incident: dict[str, Any]) -> None:
    global _ACTIVE
    _ACTIVE = incident


def _incident() -> dict[str, Any]:
    if _ACTIVE is None:
        raise RuntimeError("no active incident set — call set_active_incident() first")
    return _ACTIVE


def _signal_for(service: str) -> dict[str, Any] | None:
    """Telemetry for ``service``, or None if this incident has none for it."""
    inc = _incident()
    if service == inc["service"]:
        return inc["signal"]
    return inc["signal"].get("per_service", {}).get(service)


def query_logs(service: str, window: str = "last_30m") -> dict[str, Any]:
    """Return log snippets for a service over a time window."""
    signal = _signal_for(service)
    logs = signal["logs"] if signal else _NO_LOGS
    return {
        "service": service,
        "window": window,
        "lines": logs["lines"],
        "error_terms": logs["error_terms"],
    }


def query_metrics(service: str, window: str = "last_30m") -> dict[str, Any]:
    """Return a metric summary (error rate, latency) for a service."""
    signal = _signal_for(service)
    metrics = signal["metrics"] if signal else _NO_METRICS
    return {"service": service, "window": window, **metrics}


def get_dependency_map(service: str) -> dict[str, Any]:
    """Return the owning team + upstream dependencies for a service.

    Dependencies are leaves in these fixtures: we know who owns them (from the
    parent's map) but not what they depend on, so their upstream is empty.
    """
    dep_map = _incident()["signal"]["dependency_map"]
    if service == dep_map["service"]:
        return dict(dep_map)
    for dep in dep_map["upstream"]:
        if dep["name"] == service:
            return {"service": service, "owning_team": dep["team"], "upstream": []}
    return {"service": service, "owning_team": None, "upstream": [], "error": "unknown service"}


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
