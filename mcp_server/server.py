"""MCP server (stdio transport) wrapping the ops platform REST API.

Run with the platform API already up (see README):

    uv run python -m mcp_server.server

The tools never touch the database directly — everything goes through HTTP,
mirroring how an MCP server would wrap a real vendor API. Set OPS_PLATFORM_URL
to point at a non-default platform location.

The three task tools are backend-pluggable: OPS_TASK_BACKEND=platform (default)
serves them from the mock platform, OPS_TASK_BACKEND=clickup from the real ClickUp
API (see backends.py and the README's Task backends section). All other tools are always
platform-backed.

Writes need human approval by default (approvals.py): write tools submit a change
request and return it as pending. The server can submit and read change requests but
has no tool or code path that approves them; a person does that with
scripts/review_changes.py. Failed write calls are reported to the platform audit log
(audit.py). OPS_REQUIRE_APPROVAL=false restores direct writes.
"""

from mcp.server.fastmcp import FastMCP

from . import approvals
from .approvals import submit_write
from .audit import audited_write
from .backends import get_task_backend
from .platform_client import request, resolve_employee, resolve_project

TASK_STATUSES = ("todo", "in_progress", "done")

mcp = FastMCP("ops-platform")


# --- read tools ----------------------------------------------------------------


@mcp.tool()
async def list_employees() -> list[dict]:
    """List every employee in the platform with their id, name, role, department, and
    weekly_capacity_hours. Use this to discover who exists, look up an employee's id or
    exact name, or check someone's capacity before assigning work or interpreting a
    utilization report. Takes no arguments and returns the full employee list; if you
    need a subset, filter the result yourself."""
    return await request("GET", "/employees")


@mcp.tool()
async def list_projects() -> list[dict]:
    """List every project with its id, name, client, status (active, on_hold, or closed),
    and budget_hours. Use this to discover which projects exist or to find a project's id
    or exact name before creating tasks, logging time, or requesting an hours report.
    Takes no arguments and returns the full project list."""
    return await request("GET", "/projects")


@mcp.tool()
async def list_tasks(
    project: str | None = None,
    assignee: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """List tasks, optionally narrowed by project, assignee, and/or status. Use this to
    answer what is on someone's plate, what work remains on a project, or to find a
    task's id before updating its status. `project` and `assignee` accept a name, a
    unique name fragment, or an id; `status` must be todo, in_progress, or done. All
    filters combine with AND. Returns the matching tasks with project and assignee
    names included for readability. Tasks live in the system selected by
    OPS_TASK_BACKEND (the mock platform by default, or ClickUp)."""
    return await get_task_backend().list_tasks(project=project, assignee=assignee, status=status)


@mcp.tool()
async def get_project_hours(project: str) -> dict:
    """Report how a single project is tracking against its budget: returns the project's
    metadata plus budget_hours, logged_hours (sum of all time entries), and
    remaining_hours. Use this for questions like 'how is Orion tracking against budget'
    or before logging significant additional time. `project` accepts a name, a unique
    name fragment, or a numeric id."""
    resolved = await resolve_project(project)
    return await request("GET", f"/projects/{resolved['id']}/hours")


@mcp.tool()
async def utilization_report(week: str | None = None) -> dict:
    """Per-employee utilization for one week: logged hours against weekly capacity, with
    a utilization_pct per person. Use this for questions like 'who is over or under
    capacity' or 'pull this week's utilization'. `week` is an ISO week string such as
    '2026-W24'; omit it for the current week. Returns the week's start/end dates and one
    row per employee (including employees with zero logged hours)."""
    params = {"week": week} if week is not None else None
    return await request("GET", "/reports/utilization", params=params)


@mcp.tool()
async def get_change_request(change_request_id: int) -> dict:
    """Check a change request returned by create_task, update_task_status, or log_time.
    Use this when the user asks whether a change went through, or when you need the id of
    a task that a request creates (it appears in `result` once approved). Returns the
    status — pending (waiting for a human reviewer), approved (applied; `result` holds the
    record as written), rejected (the reviewer's reason is in decision_note), stale
    (refused because the record changed after submission), or failed — plus a summary and
    next_step guidance. You cannot approve or reject requests: only a person can, outside
    this assistant."""
    return await approvals.get_change_request(change_request_id)


# --- write tools ---------------------------------------------------------------
# By default every write returns a pending change request; see approvals.py.


@mcp.tool()
@audited_write
async def create_task(
    project: str,
    title: str,
    assignee: str | None = None,
    due_date: str | None = None,
) -> dict:
    """Create a new task on a project. Use this when asked to add a work item, to-do, or
    action item. `project` (required) and `assignee` (optional) accept a name, a unique
    name fragment, or an id; `due_date` is optional in YYYY-MM-DD format. New tasks
    always start in the 'todo' state. Writes need human approval by default: this submits
    a change request and returns its change_request_id with status 'pending'. The task
    does not exist until a person approves the request outside this assistant, so tell the
    user it is pending approval — do not call it created and do not resubmit it; its id
    appears in get_change_request once approved. (If approval mode is turned off, the
    created task is returned directly.) The task goes to the system selected by
    OPS_TASK_BACKEND (the mock platform by default, or ClickUp)."""
    return await get_task_backend().create_task(
        project=project, title=title, assignee=assignee, due_date=due_date
    )


@mcp.tool()
@audited_write
async def update_task_status(task_id: str, status: str) -> dict:
    """Move a task to a new status: todo, in_progress, or done. Use this when asked to
    start, finish, reopen, or otherwise progress a task. Requires the `task_id` — if you
    only know the task by title or assignee, call list_tasks first to find the id (ids
    are numeric on the platform backend, alphanumeric strings on ClickUp). Writes need
    human approval by default: this submits a change request and returns its
    change_request_id with status 'pending'; the task keeps its current status until a
    person approves, and the request is refused as stale if the task changes first. Tell
    the user the change is pending approval, not done. (If approval mode is turned off,
    the updated task is returned directly.)"""
    if status not in TASK_STATUSES:
        raise ValueError(f"Invalid status {status!r}. Valid statuses: {', '.join(TASK_STATUSES)}")
    return await get_task_backend().update_task_status(task_id=task_id, status=status)


@mcp.tool()
@audited_write
async def log_time(
    employee: str,
    project: str,
    date: str,
    hours: float,
    note: str | None = None,
) -> dict:
    """Log hours that an employee worked on a project for a specific day. Use this when
    someone reports time worked or asks you to record effort. `employee` and `project`
    accept a name, a unique name fragment, or a numeric id; `date` is YYYY-MM-DD; `hours`
    must be greater than 0 and at most 24; `note` is an optional short description of the
    work. Writes need human approval by default: this submits a change request and
    returns its change_request_id with status 'pending'; the hours count toward
    get_project_hours and utilization_report only after a person approves it. Tell the
    user the entry is pending approval. (If approval mode is turned off, the created time
    entry is returned directly.)"""
    payload: dict = {
        "employee_id": (await resolve_employee(employee))["id"],
        "project_id": (await resolve_project(project))["id"],
        "date": date,
        "hours": hours,
    }
    if note is not None:
        payload["note"] = note
    arguments = {
        "employee": employee,
        "project": project,
        "date": date,
        "hours": hours,
        "note": note,
    }
    return await submit_write("log_time", "create_time_entry", payload, arguments)


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
