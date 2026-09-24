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
"""

from mcp.server.fastmcp import FastMCP

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


# --- write tools ---------------------------------------------------------------


@mcp.tool()
async def create_task(
    project: str,
    title: str,
    assignee: str | None = None,
    due_date: str | None = None,
) -> dict:
    """Create a new task on a project. Use this when asked to add a work item, to-do, or
    action item. `project` (required) and `assignee` (optional) accept a name, a unique
    name fragment, or an id; `due_date` is optional in YYYY-MM-DD format. New tasks
    always start in the 'todo' state — use update_task_status afterwards if a different
    status is needed. Returns the created task including its id; mention the id so the
    user can refer to the task later. The task is created in the system selected by
    OPS_TASK_BACKEND (the mock platform by default, or ClickUp)."""
    return await get_task_backend().create_task(
        project=project, title=title, assignee=assignee, due_date=due_date
    )


@mcp.tool()
async def update_task_status(task_id: str, status: str) -> dict:
    """Move a task to a new status: todo, in_progress, or done. Use this when asked to
    start, finish, reopen, or otherwise progress a task. Requires the `task_id` — if you
    only know the task by title or assignee, call list_tasks first to find the id (ids
    are numeric on the platform backend, alphanumeric strings on ClickUp). Returns the
    full updated task so you can confirm the change took effect."""
    if status not in TASK_STATUSES:
        raise ValueError(f"Invalid status {status!r}. Valid statuses: {', '.join(TASK_STATUSES)}")
    return await get_task_backend().update_task_status(task_id=task_id, status=status)


@mcp.tool()
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
    work. Returns the created time entry including its id. Logged time immediately shows
    up in get_project_hours and utilization_report."""
    payload: dict = {
        "employee_id": (await resolve_employee(employee))["id"],
        "project_id": (await resolve_project(project))["id"],
        "date": date,
        "hours": hours,
    }
    if note is not None:
        payload["note"] = note
    return await request("POST", "/time-entries", json=payload)


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
