"""CLI: run the triage agent on one synthetic incident.

    python -m sre_triage --list
    python -m sre_triage --incident checkout-db-pool            # real Claude (default)
    python -m sre_triage --incident checkout-db-pool --model mock
"""

from __future__ import annotations

import argparse
import json
import sys

from .agent import diagnose
from .fixtures import INCIDENTS, by_id
from .tools import set_active_incident


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sre_triage", description="SRE incident-triage agent")
    p.add_argument("--incident", help="incident id from the fixtures (see --list)")
    p.add_argument("--model", default="anthropic", choices=["anthropic", "mock"],
                   help="model backend (default: anthropic; requires ANTHROPIC_API_KEY)")
    p.add_argument("--list", action="store_true", help="list available incident ids and exit")
    args = p.parse_args(argv)

    if args.list or not args.incident:
        print("Available incidents:")
        for inc in INCIDENTS:
            print(f"  {inc['id']:<26} [{inc['difficulty']:<9}] {inc['service']}")
        return 0

    try:
        incident = by_id(args.incident)
    except KeyError:
        print(f"unknown incident: {args.incident!r} (try --list)", file=sys.stderr)
        return 2

    set_active_incident(incident)
    try:
        diag = diagnose(incident["scenario"], incident["service"], incident["window"], backend=args.model)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(f"\nIncident: {incident['id']}  ({incident['difficulty']})")
    print(f"Service:  {incident['service']}")
    print(f"Scenario: {incident['scenario']}\n")
    print("Diagnosis:")
    print(json.dumps(diag.to_dict(), indent=2))
    print(f"\nExpected escalate_to: {incident['label']['expected_escalate_to']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
