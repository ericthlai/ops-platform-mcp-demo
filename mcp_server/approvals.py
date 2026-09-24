"""Approval mode: platform write tools submit change requests instead of writing.

On by default. A change request is validated by the platform at submission (unknown ids
and invalid values still fail immediately) but nothing changes until a human approves it
with ``scripts/review_changes.py``. This module can submit and read requests; it has no
code path to the platform's ``/admin`` review routes, and no MCP tool exposes one.

Set ``OPS_REQUIRE_APPROVAL=false`` to restore direct writes (the pre-approval behavior).
"""

import os
from typing import Any

from . import platform_client

ACTOR = "mcp-agent"

# Direct-write routes used only when approval mode is off.
DIRECT_ROUTES = {
    "create_task": ("POST", "/tasks"),
    "update_task": ("PATCH", "/tasks/{target_id}"),
    "create_time_entry": ("POST", "/time-entries"),
}

NEXT_STEPS = {
    "pending": (
        "Waiting for a human reviewer; nothing has changed yet. Tell the user the change "
        "request id. Only a person can approve or reject it, outside this assistant; check "
        "progress later with get_change_request."
    ),
    "approved": "Approved and applied; `result` is the record as written.",
    "rejected": "Rejected by a reviewer (reason in decision_note). Nothing was changed.",
    "stale": (
        "Not applied: the record changed after the request was submitted (see "
        "decision_note). Submit a new request if the change is still wanted."
    ),
    "failed": "Approval could not be applied (see decision_note). Nothing was changed.",
}


def approval_required() -> bool:
    value = os.environ.get("OPS_REQUIRE_APPROVAL", "true").strip().lower()
    return value not in {"false", "0", "no", "off"}


def caller_headers(tool: str) -> dict[str, str]:
    """Identify the MCP server and tool to the platform's audit log."""
    return {"X-Ops-Actor": ACTOR, "X-Ops-Tool": tool}


def change_request_view(change: dict) -> dict:
    """The model-facing shape of a change request."""
    return {
        "change_request_id": change["id"],
        "status": change["status"],
        "summary": change["summary"],
        "requested_at": change["requested_at"],
        "decided_by": change["decided_by"],
        "decided_at": change["decided_at"],
        "decision_note": change["decision_note"],
        "result": change["result"],
        "next_step": NEXT_STEPS.get(change["status"], ""),
    }


async def submit_write(
    tool: str,
    action: str,
    payload: dict[str, Any],
    arguments: dict[str, Any],
    target_id: int | str | None = None,
) -> dict:
    """Queue the write for approval (default) or, with approval off, apply it directly."""
    headers = caller_headers(tool)
    if approval_required():
        body = {
            "action": action,
            "target_id": target_id,
            "payload": payload,
            "arguments": arguments,
        }
        change = await platform_client.request(
            "POST", "/change-requests", json=body, headers=headers
        )
        return change_request_view(change)
    method, path = DIRECT_ROUTES[action]
    return await platform_client.request(
        method, path.format(target_id=target_id), json=payload, headers=headers
    )


async def get_change_request(change_request_id: int) -> dict:
    change = await platform_client.request("GET", f"/change-requests/{change_request_id}")
    return change_request_view(change)
