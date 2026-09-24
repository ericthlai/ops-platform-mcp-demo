"""MCP tool handler tests.

The platform API is fixtured, not mocked away: tools talk over httpx.ASGITransport
to the real FastAPI app backed by the seeded in-memory test DB. That exercises the
full tool → HTTP → API → DB path without a running server. Tools are async, so each
test drives them with asyncio.run().

Write tools run in approval mode (the default) unless a test uses `direct_writes`.
"""

import asyncio
import re
from pathlib import Path

import httpx
import pytest

import mcp_server
from mcp_server import platform_client, server
from platform_api.seed import TASKS, TIME_ENTRIES


@pytest.fixture
def tools(session_override, monkeypatch):
    """Point the MCP server's HTTP layer at the in-process app."""
    from platform_api.main import app

    monkeypatch.setattr(platform_client, "_transport", httpx.ASGITransport(app=app))
    monkeypatch.setattr(platform_client, "PLATFORM_URL", "http://platform.test")
    monkeypatch.delenv("OPS_REQUIRE_APPROVAL", raising=False)
    monkeypatch.delenv("OPS_TASK_BACKEND", raising=False)
    return server


@pytest.fixture
def direct_writes(tools, monkeypatch):
    """The pre-approval behavior: write tools apply changes immediately."""
    monkeypatch.setenv("OPS_REQUIRE_APPROVAL", "false")
    return tools


def run(coro):
    return asyncio.run(coro)


def approve(client, change_request_id, reviewer="reviewer-a"):
    return client.post(
        f"/admin/change-requests/{change_request_id}/approve", json={"reviewer": reviewer}
    )


def task_status(client, task_id):
    return next(t["status"] for t in client.get("/tasks").json() if t["id"] == task_id)


def audit_log(client):
    return client.get("/audit-events").json()


# --- registration ---------------------------------------------------------------


def test_all_nine_tools_registered_with_descriptions():
    tools = run(server.mcp.list_tools())
    assert {t.name for t in tools} == {
        "list_employees",
        "list_projects",
        "list_tasks",
        "get_project_hours",
        "utilization_report",
        "get_change_request",
        "create_task",
        "update_task_status",
        "log_time",
    }
    for tool in tools:
        assert tool.description and len(tool.description) > 80, (
            f"{tool.name} needs a real model-facing description"
        )


def test_no_tool_can_decide_requests_or_touch_the_audit_log():
    names = [t.name for t in run(server.mcp.list_tools())]
    assert not [n for n in names if re.search(r"approve|reject|decide|review|audit|delete", n)]


def test_mcp_server_has_no_code_path_to_the_review_routes():
    """No request path in the MCP package points at the platform's /admin review routes."""
    package = Path(mcp_server.__file__).parent
    for source in package.glob("*.py"):
        assert not re.search(r"[\"']/admin", source.read_text(encoding="utf-8")), source.name


def test_write_tool_signatures_survive_the_audit_decorator():
    schemas = {t.name: t.inputSchema for t in run(server.mcp.list_tools())}
    assert schemas["log_time"]["required"] == ["employee", "project", "date", "hours"]
    assert set(schemas["create_task"]["properties"]) == {
        "project",
        "title",
        "assignee",
        "due_date",
    }


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


# --- write tools: approval mode (default) ---------------------------------------------


def test_create_task_submits_pending_change_with_resolved_names(tools, client):
    change = run(
        tools.create_task(
            project="Phoenix",
            title="Review onboarding copy",
            assignee="Felix",
            due_date="2026-07-01",
        )
    )
    assert change["status"] == "pending"
    assert change["change_request_id"] == 1
    assert change["summary"] == (
        "Create task 'Review onboarding copy' on Phoenix Onboarding Portal, "
        "assigned to Felix Grant, due 2026-07-01"
    )
    assert "nothing has changed yet" in change["next_step"]
    assert len(client.get("/tasks").json()) == len(TASKS)
    stored = client.get("/change-requests/1").json()
    assert stored["payload"] == {
        "project_id": 4,
        "title": "Review onboarding copy",
        "assignee_id": 8,
        "due_date": "2026-07-01",
    }
    assert stored["requested_by"] == "mcp-agent"
    assert stored["arguments"]["assignee"] == "Felix"


def test_update_task_status_is_pending_until_approved(tools, client):
    change = run(tools.update_task_status(task_id="1", status="in_progress"))
    assert change["status"] == "pending"
    assert task_status(client, 1) == "done"
    approve(client, change["change_request_id"])
    assert task_status(client, 1) == "in_progress"


def test_log_time_counts_only_after_approval(tools, client):
    seed_total = sum(hours for _, project_id, _, _, hours, _ in TIME_ENTRIES if project_id == 1)
    change = run(
        tools.log_time(employee="Marcus", project="Orion", date="2026-06-10", hours=4, note="Fixes")
    )
    assert change["summary"] == (
        "Log 4h for Marcus Webb on Orion Data Migration on 2026-06-10, note 'Fixes'"
    )
    assert run(tools.get_project_hours("Orion"))["logged_hours"] == seed_total
    approve(client, change["change_request_id"])
    assert run(tools.get_project_hours("Orion"))["logged_hours"] == seed_total + 4


def test_get_change_request_reports_approval_result(tools, client):
    change = run(tools.create_task(project="Atlas", title="Refresh the KPI deck", assignee="Tessa"))
    approve(client, change["change_request_id"])
    status = run(tools.get_change_request(change["change_request_id"]))
    assert status["status"] == "approved"
    assert status["decided_by"] == "reviewer-a"
    assert status["result"]["id"] == len(TASKS) + 1
    assert status["result"]["assignee_id"] == 5


def test_get_change_request_reports_rejection_reason(tools, client):
    change = run(tools.update_task_status(task_id="4", status="done"))
    client.post(
        f"/admin/change-requests/{change['change_request_id']}/reject",
        json={"reviewer": "reviewer-a", "reason": "Still in review"},
    )
    status = run(tools.get_change_request(change["change_request_id"]))
    assert status["status"] == "rejected"
    assert status["decision_note"] == "Still in review"
    assert "Nothing was changed" in status["next_step"]


def test_get_change_request_reports_stale_refusal(tools, client):
    first = run(tools.update_task_status(task_id="4", status="in_progress"))
    second = run(tools.update_task_status(task_id="4", status="done"))
    approve(client, first["change_request_id"])
    assert approve(client, second["change_request_id"]).status_code == 409
    status = run(tools.get_change_request(second["change_request_id"]))
    assert status["status"] == "stale"
    assert "changed after this request was submitted" in status["decision_note"]


def test_get_change_request_missing_is_404(tools):
    with pytest.raises(ValueError, match="404.*Change request 42 not found"):
        run(tools.get_change_request(42))


def test_update_task_status_rejects_bad_status(tools):
    with pytest.raises(ValueError, match="Valid statuses: todo, in_progress, done"):
        run(tools.update_task_status(task_id=1, status="blocked"))


def test_update_task_status_missing_task_fails_at_submission(tools, client):
    with pytest.raises(ValueError, match="404.*Task 9999 not found"):
        run(tools.update_task_status(task_id=9999, status="done"))
    assert client.get("/change-requests").json() == []


def test_log_time_invalid_hours_fails_at_submission(tools, client):
    with pytest.raises(ValueError, match="422"):
        run(tools.log_time(employee="Marcus Webb", project="Orion", date="2026-06-10", hours=30))
    assert client.get("/change-requests").json() == []


# --- audit of write calls ---------------------------------------------------------------


def test_failed_write_is_audited_with_arguments_and_error(tools, client):
    with pytest.raises(ValueError, match="No employee found matching 'Zelda'"):
        run(tools.create_task(project="Phoenix", title="Mystery task", assignee="Zelda"))
    [event] = audit_log(client)
    assert event["outcome"] == "error"
    assert event["actor"] == "mcp-agent"
    assert event["tool"] == "create_task"
    assert event["arguments"] == {
        "project": "Phoenix",
        "title": "Mystery task",
        "assignee": "Zelda",
        "due_date": None,
    }
    assert "No employee found matching 'Zelda'" in event["detail"]


def test_invalid_status_and_platform_404_are_audited(tools, client):
    for task_id, status in (("1", "blocked"), ("9999", "done")):
        with pytest.raises(ValueError):
            run(tools.update_task_status(task_id=task_id, status=status))
    missing, invalid = audit_log(client)  # newest first
    assert "Invalid status 'blocked'" in invalid["detail"]
    assert "Platform API error 404" in missing["detail"]


def test_each_write_call_leaves_one_audit_row(tools, client):
    change = run(tools.log_time(employee="Marcus", project="Orion", date="2026-06-10", hours=1))
    [submitted] = audit_log(client)
    assert submitted["outcome"] == "pending"
    assert submitted["tool"] == "log_time"
    assert submitted["change_request_id"] == change["change_request_id"]
    assert submitted["arguments"]["employee"] == "Marcus"


def test_audit_report_failure_does_not_mask_the_tool_error(tools, monkeypatch):
    def platform_down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    monkeypatch.setattr(platform_client, "_transport", httpx.MockTransport(platform_down))
    with pytest.raises(ValueError, match="Cannot reach the platform API"):
        run(tools.log_time(employee="Marcus", project="Orion", date="2026-06-10", hours=1))


# --- write tools: approval mode off ---------------------------------------------------


def test_create_task_resolves_names(direct_writes):
    task = run(
        direct_writes.create_task(
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


def test_update_task_status(direct_writes):
    updated = run(direct_writes.update_task_status(task_id=1, status="in_progress"))
    assert updated["id"] == 1
    assert updated["status"] == "in_progress"


def test_update_task_status_missing_task_surfaces_api_404(direct_writes):
    with pytest.raises(ValueError, match="404"):
        run(direct_writes.update_task_status(task_id=9999, status="done"))


def test_log_time_resolves_names_and_returns_entry(direct_writes):
    entry = run(
        direct_writes.log_time(
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
    report = run(direct_writes.get_project_hours("Orion"))
    seed_total = sum(hours for _, project_id, _, _, hours, _ in TIME_ENTRIES if project_id == 1)
    assert report["logged_hours"] == seed_total + 4


def test_log_time_invalid_hours_surfaces_api_422(direct_writes):
    with pytest.raises(ValueError, match="422"):
        run(
            direct_writes.log_time(
                employee="Marcus Webb", project="Orion", date="2026-06-10", hours=0
            )
        )


def test_direct_write_is_audited_by_the_platform(direct_writes, client):
    run(direct_writes.update_task_status(task_id="4", status="done"))
    [event] = audit_log(client)
    assert event["outcome"] == "applied"
    assert event["actor"] == "mcp-agent"
    assert event["tool"] == "update_task_status"
    assert event["before"]["status"] == "todo"
    assert event["after"]["status"] == "done"
