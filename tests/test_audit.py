"""Audit log: direct writes are recorded in the same transaction, events are read-only
through the API, and clients can only report tool errors and external writes."""

MCP_HEADERS = {"X-Ops-Actor": "mcp-agent", "X-Ops-Tool": "update_task_status"}


def latest_event(client):
    return client.get("/audit-events", params={"limit": 1}).json()[0]


def test_direct_task_update_records_before_and_after(client):
    client.patch("/tasks/4", json={"status": "done"}, headers=MCP_HEADERS)
    event = latest_event(client)
    assert event["outcome"] == "applied"
    assert event["actor"] == "mcp-agent"
    assert event["tool"] == "update_task_status"
    assert event["action"] == "update_task"
    assert event["target"] == "task 4"
    assert event["arguments"] == {"status": "done"}
    assert event["before"]["status"] == "todo"
    assert event["after"]["status"] == "done"
    assert event["occurred_at"].endswith("+00:00")


def test_direct_create_defaults_actor_to_api(client):
    created = client.post("/tasks", json={"project_id": 1, "title": "Direct"}).json()
    event = latest_event(client)
    assert event["actor"] == "api"
    assert event["tool"] is None
    assert event["target"] == f"task {created['id']}"
    assert event["before"] is None
    assert event["after"] == created


def test_time_entry_employee_and_project_writes_are_audited(client):
    entry = client.post(
        "/time-entries",
        json={"employee_id": 2, "project_id": 1, "date": "2026-06-10", "hours": 2},
    ).json()
    assert latest_event(client)["target"] == f"time entry {entry['id']}"
    client.post(
        "/employees",
        json={
            "name": "Iris Vale",
            "role": "Consultant",
            "department": "Strategy",
            "weekly_capacity_hours": 40,
        },
    )
    assert latest_event(client)["action"] == "create_employee"
    client.post("/projects", json={"name": "Nova", "client": "Client Z", "budget_hours": 10})
    assert latest_event(client)["action"] == "create_project"


def test_failed_direct_write_leaves_no_event(client):
    assert client.patch("/tasks/999", json={"status": "done"}).status_code == 404
    assert client.get("/audit-events").json() == []


def test_seed_starts_with_empty_audit_log(client):
    assert client.get("/audit-events").json() == []


def test_audit_events_newest_first_with_limit(client):
    for task_id in (1, 2, 3):
        client.patch(f"/tasks/{task_id}", json={"status": "done"})
    events = client.get("/audit-events", params={"limit": 2}).json()
    assert [e["target"] for e in events] == ["task 3", "task 2"]
    assert client.get("/audit-events", params={"limit": 0}).status_code == 422


def test_report_tool_error(client):
    response = client.post(
        "/audit-events",
        json={
            "tool": "log_time",
            "arguments": {"employee": "Zelda", "project": "Orion"},
            "outcome": "error",
            "detail": "No employee found matching 'Zelda'",
        },
        headers={"X-Ops-Actor": "mcp-agent"},
    )
    assert response.status_code == 201
    event = latest_event(client)
    assert event["outcome"] == "error"
    assert event["tool"] == "log_time"
    assert event["actor"] == "mcp-agent"
    assert event["detail"] == "No employee found matching 'Zelda'"


def test_report_external_write(client):
    response = client.post(
        "/audit-events",
        json={"tool": "create_task", "outcome": "external", "result": {"id": "abc123"}},
    )
    assert response.status_code == 201
    assert latest_event(client)["after"] == {"id": "abc123"}


def test_clients_cannot_report_decisions_or_applied_writes(client):
    for outcome in ("approved", "applied", "pending", "rejected"):
        response = client.post("/audit-events", json={"tool": "create_task", "outcome": outcome})
        assert response.status_code == 422
    assert client.get("/audit-events").json() == []


def test_audit_events_cannot_be_edited_or_deleted(client):
    client.patch("/tasks/1", json={"status": "done"})
    event_id = latest_event(client)["id"]
    assert client.delete(f"/audit-events/{event_id}").status_code in (404, 405)
    assert client.patch(f"/audit-events/{event_id}", json={}).status_code in (404, 405)
    assert client.delete("/audit-events").status_code == 405
