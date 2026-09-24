"""MCP tool handler tests.

The platform API is fixtured, not mocked away: tools talk over httpx.ASGITransport
to the real FastAPI app backed by the seeded in-memory test DB. That exercises the
full tool → HTTP → API → DB path without a running server. Tools are async, so each
test drives them with asyncio.run().
"""

import asyncio

import httpx
import pytest

from mcp_server import platform_client, server
from platform_api.seed import TIME_ENTRIES


@pytest.fixture
def tools(session_override, monkeypatch):
    """Point the MCP server's HTTP layer at the in-process app."""
    from platform_api.main import app

    monkeypatch.setattr(platform_client, "_transport", httpx.ASGITransport(app=app))
    monkeypatch.setattr(platform_client, "PLATFORM_URL", "http://platform.test")
    return server


def run(coro):
    return asyncio.run(coro)


# --- registration ---------------------------------------------------------------


def test_all_eight_tools_registered_with_descriptions():
    tools = run(server.mcp.list_tools())
    assert {t.name for t in tools} == {
        "list_employees",
        "list_projects",
        "list_tasks",
        "get_project_hours",
        "utilization_report",
        "create_task",
        "update_task_status",
        "log_time",
    }
    for tool in tools:
        assert tool.description and len(tool.description) > 80, (
            f"{tool.name} needs a real model-facing description"
        )


# --- read tools -------------------------------------------------------------------


def test_list_employees(tools):
    employees = run(tools.list_employees())
    assert len(employees) == 8
    assert {"id", "name", "role", "department", "weekly_capacity_hours"} <= employees[0].keys()


def test_list_projects(tools):
    projects = run(tools.list_projects())
    assert {p["name"] for p in projects} >= {"Orion Data Migration", "Atlas KPI Dashboard"}


def test_list_tasks_filters_and_enriches(tools):
    tasks = run(tools.list_tasks(project="Atlas", status="in_progress"))
    assert tasks, "seed has in_progress tasks on Atlas KPI Dashboard"
    assert all(t["status"] == "in_progress" for t in tasks)
    assert all(t["project_name"] == "Atlas KPI Dashboard" for t in tasks)
    assert all("assignee_name" in t for t in tasks)


def test_list_tasks_by_assignee_name(tools):
    tasks = run(tools.list_tasks(assignee="Tessa"))
    assert tasks
    assert all(t["assignee_name"] == "Tessa Morgan" for t in tasks)


def test_get_project_hours_by_name_fragment(tools):
    report = run(tools.get_project_hours("orion"))
    expected = sum(hours for _, project_id, _, _, hours, _ in TIME_ENTRIES if project_id == 1)
    assert report["project_name"] == "Orion Data Migration"
    assert report["logged_hours"] == expected


def test_utilization_report_default_week(tools):
    report = run(tools.utilization_report())
    assert len(report["employees"]) == 8
    marcus = next(r for r in report["employees"] if r["name"] == "Marcus Webb")
    expected = sum(
        hours
        for employee_id, _, week_offset, _, hours, _ in TIME_ENTRIES
        if employee_id == 2 and week_offset == 0
    )
    assert marcus["logged_hours"] == expected


# --- name resolution ----------------------------------------------------------------


def test_resolution_is_case_insensitive_exact(tools):
    report = run(tools.get_project_hours("ATLAS KPI DASHBOARD"))
    assert report["project_id"] == 2


def test_resolution_by_numeric_id_string(tools):
    report = run(tools.get_project_hours("3"))
    assert report["project_name"] == "Quartz CRM Cleanup"


def test_ambiguous_name_lists_candidates(tools):
    with pytest.raises(ValueError, match="Ambiguous project 'a'"):
        run(tools.get_project_hours("a"))


def test_unknown_name_lists_known_names(tools):
    with pytest.raises(ValueError, match="No employee found matching 'Zelda'"):
        run(tools.log_time(employee="Zelda", project="Orion", date="2026-06-10", hours=2))


# --- write tools ----------------------------------------------------------------------


def test_create_task_resolves_names(tools):
    task = run(
        tools.create_task(
            project="Phoenix",
            title="Review onboarding copy",
            assignee="Felix",
            due_date="2026-07-01",
        )
    )
    assert task["id"] is not None
    assert task["project_id"] == 4
    assert task["assignee_id"] == 8
    assert task["status"] == "todo"


def test_update_task_status(tools):
    updated = run(tools.update_task_status(task_id=1, status="in_progress"))
    assert updated["id"] == 1
    assert updated["status"] == "in_progress"


def test_update_task_status_rejects_bad_status(tools):
    with pytest.raises(ValueError, match="Valid statuses: todo, in_progress, done"):
        run(tools.update_task_status(task_id=1, status="blocked"))


def test_update_task_status_missing_task_surfaces_api_404(tools):
    with pytest.raises(ValueError, match="404"):
        run(tools.update_task_status(task_id=9999, status="done"))


def test_log_time_resolves_names_and_returns_entry(tools):
    entry = run(
        tools.log_time(
            employee="Marcus Webb",
            project="Orion",
            date="2026-06-10",
            hours=4,
            note="Pipeline fixes",
        )
    )
    assert entry["id"] is not None
    assert entry["employee_id"] == 2
    assert entry["project_id"] == 1
    # the new entry is visible through the reporting tool
    report = run(tools.get_project_hours("Orion"))
    seed_total = sum(hours for _, project_id, _, _, hours, _ in TIME_ENTRIES if project_id == 1)
    assert report["logged_hours"] == seed_total + 4


def test_log_time_invalid_hours_surfaces_api_422(tools):
    with pytest.raises(ValueError, match="422"):
        run(tools.log_time(employee="Marcus Webb", project="Orion", date="2026-06-10", hours=0))
