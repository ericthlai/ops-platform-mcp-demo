"""Human review of change requests: the approval path the assistant cannot reach.

The MCP server can submit change requests and read their status. Approving or rejecting
one goes through the platform's /admin routes, which this CLI calls and no MCP tool does.

    uv run python scripts/review_changes.py list              # pending requests
    uv run python scripts/review_changes.py list --status all
    uv run python scripts/review_changes.py show 1
    uv run python scripts/review_changes.py approve 1 --reviewer ops-lead
    uv run python scripts/review_changes.py reject 2 --reason "Hours belong on Atlas"
    uv run python scripts/review_changes.py audit --limit 10

The reviewer name recorded on decisions comes from --reviewer, else OPS_REVIEWER, else
"reviewer". The platform refuses a decision by the same name that submitted the request.
Set OPS_PLATFORM_URL for a non-default platform location.
"""

import argparse
import json
import os
import sys

import httpx

PLATFORM_URL = os.environ.get("OPS_PLATFORM_URL", "http://127.0.0.1:8000")
STATUSES = ("pending", "approved", "rejected", "stale", "failed", "all")


class ReviewError(Exception):
    pass


def call(client: httpx.Client, method: str, path: str, **kwargs) -> dict | list:
    try:
        response = client.request(method, path, **kwargs)
    except httpx.ConnectError:
        raise ReviewError(
            f"Cannot reach the platform API at {PLATFORM_URL}. "
            "Start it with: uv run uvicorn platform_api.main:app"
        ) from None
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise ReviewError(f"HTTP {response.status_code}: {detail}")
    return response.json()


def describe_result(change: dict) -> str:
    result = change.get("result")
    if not result:
        return ""
    kind = "time entry" if change["action"] == "create_time_entry" else "task"
    status = f" (status {result['status']})" if "status" in result else ""
    return f" -> {kind} {result['id']}{status}"


def format_change(change: dict) -> str:
    line = (
        f"#{change['id']:<3} {change['status']:<9} {change['requested_at']}  "
        f"by {change['requested_by']}  {change['summary']}"
    )
    if change.get("decided_by"):
        line += f"\n     {change['status']} by {change['decided_by']} at {change['decided_at']}"
        if change.get("decision_note"):
            line += f": {change['decision_note']}"
    return line


def format_event(event: dict) -> str:
    parts = [
        f"#{event['id']:<3}",
        event["occurred_at"],
        f"{event['actor']:<12}",
        f"{event['outcome']:<8}",
        event["tool"] or event["action"],
    ]
    if event.get("target"):
        parts.append(event["target"])
    if event.get("change_request_id"):
        parts.append(f"request #{event['change_request_id']}")
    line = "  ".join(parts)
    if event.get("detail"):
        line += f"\n     {event['detail']}"
    return line


def run_command(args: argparse.Namespace, client: httpx.Client) -> str:
    reviewer = getattr(args, "reviewer", None) or os.environ.get("OPS_REVIEWER") or "reviewer"
    if args.command == "list":
        params = {} if args.status == "all" else {"status": args.status}
        changes = call(client, "GET", "/change-requests", params=params)
        if not changes:
            return f"No {'' if args.status == 'all' else args.status + ' '}change requests."
        return "\n".join(format_change(change) for change in changes)
    if args.command == "show":
        change = call(client, "GET", f"/change-requests/{args.id}")
        return (
            format_change(change)
            + "\n"
            + json.dumps(
                {k: change[k] for k in ("arguments", "payload", "baseline", "result")}, indent=2
            )
        )
    if args.command == "approve":
        change = call(
            client,
            "POST",
            f"/admin/change-requests/{args.id}/approve",
            json={"reviewer": reviewer},
        )
        return f"approved #{change['id']}: {change['summary']}{describe_result(change)}"
    if args.command == "reject":
        change = call(
            client,
            "POST",
            f"/admin/change-requests/{args.id}/reject",
            json={"reviewer": reviewer, "reason": args.reason},
        )
        return f"rejected #{change['id']}: {change['decision_note']}"
    params = {"limit": args.limit}
    if args.change_request is not None:
        params["change_request_id"] = args.change_request
    events = call(client, "GET", "/audit-events", params=params)
    return "\n".join(format_event(event) for event in reversed(events)) or "No audit events."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    decision = argparse.ArgumentParser(add_help=False)
    decision.add_argument("id", type=int)
    decision.add_argument("--reviewer", help="name recorded on the decision")
    commands = parser.add_subparsers(dest="command", required=True)
    list_cmd = commands.add_parser("list", help="list change requests")
    list_cmd.add_argument("--status", choices=STATUSES, default="pending")
    commands.add_parser("show", help="show one request in full").add_argument("id", type=int)
    commands.add_parser("approve", parents=[decision], help="apply a pending request")
    reject = commands.add_parser("reject", parents=[decision], help="decline a pending request")
    reject.add_argument("--reason", required=True)
    audit = commands.add_parser("audit", help="show recent audit events, oldest first")
    audit.add_argument("--limit", type=int, default=20)
    audit.add_argument("--change-request", type=int, help="only events for this request")
    return parser


def main(argv: list[str] | None = None, client: httpx.Client | None = None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        if client is not None:
            print(run_command(args, client))
        else:
            with httpx.Client(base_url=PLATFORM_URL, timeout=10.0) as http:
                print(run_command(args, http))
    except ReviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
