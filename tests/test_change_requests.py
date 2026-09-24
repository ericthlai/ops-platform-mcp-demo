"""Change-request lifecycle on the platform API: submit -> human decision -> audit.

Submission must validate like a direct write and change nothing; approval must apply the
change exactly once, refuse stale or self-reviewed requests, and leave an audit trail.
"""

import pytest

from platform_api.seed import TASKS

MCP_HEADERS = {"X-Ops-Actor": "mcp-agent", "X-Ops-Tool": "create_task"}


def submit(client, action, payload, target_id=None, arguments=None, headers=MCP_HEADERS):
    body = {"action": action, "payload": payload, "target_id": target_id, "arguments": arguments}
    return client.post("/change-requests", json=body, headers=headers)


def approve(client, request_id, reviewer="reviewer-a"):
    return client.post(f"/admin/change-requests/{request_id}/approve", json={"reviewer": reviewer})


def reject(client, request_id, reason, reviewer="reviewer-a"):
    return client.post(
        f"/admin/change-requests/{request_id}/reject",
        json={"reviewer": reviewer, "reason": reason},
    )


def task_status(client, task_id):
    return next(t["status"] for t in client.get("/tasks").json() if t["id"] == task_id)


def audit_events(client, request_id):
    return client.get("/audit-events", params={"change_request_id": request_id}).json()


# --- submission --------------------------------------------------------------------


def test_submit_create_task_is_pending_and_changes_nothing(client):
    response = submit(
        client,
        "create_task",
        {"project_id": 2, "title": "Refresh the KPI deck", "assignee_id": 5},
        arguments={"project": "Atlas", "title": "Refresh the KPI deck", "assignee": "Tessa"},
    )
    assert response.status_code == 201
    change = response.json()
    assert change["status"] == "pending"
    assert change["requested_by"] == "mcp-agent"
    assert change["requested_via"] == "create_task"
    assert change["summary"] == (
        "Create task 'Refresh the KPI deck' on Atlas KPI Dashboard, assigned to Tessa Morgan"
    )
    assert change["payload"] == {"project_id": 2, "title": "Refresh the KPI deck", "assignee_id": 5}
    assert len(client.get("/tasks").json()) == len(TASKS)


def test_submit_records_pending_audit_event_with_raw_arguments(client):
    arguments = {"project": "Atlas", "title": "Deck", "assignee": None, "due_date": None}
    change = submit(
        client, "create_task", {"project_id": 2, "title": "Deck"}, arguments=arguments
    ).json()
    [event] = audit_events(client, change["id"])
    assert event["outcome"] == "pending"
    assert event["actor"] == "mcp-agent"
    assert event["tool"] == "create_task"
    assert event["arguments"] == arguments
    assert event["detail"] == change["summary"]


def test_submit_update_snapshots_baseline(client):
    change = submit(client, "update_task", {"status": "done"}, target_id=4).json()
    assert change["baseline"]["id"] == 4
    assert change["baseline"]["status"] == "todo"
    assert change["summary"] == "Update task 4 'Document rollback procedure': status todo -> done"
    assert task_status(client, 4) == "todo"


def test_submit_time_entry_summary(client):
    change = submit(
        client,
        "create_time_entry",
        {"employee_id": 2, "project_id": 1, "date": "2026-06-10", "hours": 3, "note": "Fixes"},
    ).json()
    assert change["summary"] == (
        "Log 3h for Marcus Webb on Orion Data Migration on 2026-06-10, note 'Fixes'"
    )


def test_submit_quotes_caller_text_in_summaries(client):
    entry = submit(
        client,
        "create_time_entry",
        {"employee_id": 2, "project_id": 1, "date": "2026-06-10", "hours": 3, "note": "a\nb"},
    ).json()
    renamed = submit(client, "update_task", {"title": "Plan\n#9 approved"}, target_id=4).json()
    assert entry["summary"].endswith(", note 'a\\nb'")
    assert renamed["summary"] == (
        "Update task 4 'Document rollback procedure': "
        "title 'Document rollback procedure' -> 'Plan\\n#9 approved'"
    )


def test_submit_missing_task_404(client):
    response = submit(client, "update_task", {"status": "done"}, target_id=999)
    assert response.status_code == 404
    assert response.json()["detail"] == "Task 999 not found"


def test_submit_missing_assignee_404(client):
    response = submit(client, "create_task", {"project_id": 1, "title": "x", "assignee_id": 99})
    assert response.status_code == 404


def test_submit_invalid_hours_422_points_at_payload(client):
    response = submit(
        client,
        "create_time_entry",
        {"employee_id": 2, "project_id": 1, "date": "2026-06-10", "hours": 30},
    )
    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert error["loc"] == ["body", "payload", "hours"]


def test_submit_invalid_status_422(client):
    response = submit(client, "update_task", {"status": "blocked"}, target_id=1)
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("action", "payload", "target_id", "message"),
    [
        ("update_task", {"status": "done"}, None, "requires target_id"),
        ("create_task", {"project_id": 1, "title": "x"}, 3, "does not take target_id"),
        ("update_task", {}, 1, "no fields to change"),
    ],
)
def test_submit_rejects_malformed_targets(client, action, payload, target_id, message):
    response = submit(client, action, payload, target_id=target_id)
    assert response.status_code == 422
    assert message in response.json()["detail"]


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "todo"},
        {"status": "todo", "title": "Document rollback procedure"},
    ],
)
def test_submit_update_that_changes_nothing_is_422(client, payload):
    response = submit(client, "update_task", payload, target_id=4)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail.startswith("No change: task 4 already has ")
    assert "status 'todo'" in detail
    assert detail.endswith("Nothing was queued.")
    assert client.get("/change-requests").json() == []
    assert client.get("/audit-events").json() == []


def test_submit_update_with_one_real_change_is_queued(client):
    response = submit(
        client,
        "update_task",
        {"status": "done", "title": "Document rollback procedure"},
        target_id=4,
    )
    assert response.status_code == 201
    assert response.json()["payload"] == {"status": "done", "title": "Document rollback procedure"}


def test_rejected_submission_leaves_no_request(client):
    submit(client, "update_task", {"status": "done"}, target_id=999)
    assert client.get("/change-requests").json() == []


def test_list_and_get_change_requests(client):
    first = submit(client, "update_task", {"status": "done"}, target_id=4).json()
    second = submit(client, "update_task", {"status": "in_progress"}, target_id=5).json()
    reject(client, second["id"], "Not yet")
    pending = client.get("/change-requests", params={"status": "pending"}).json()
    assert [c["id"] for c in pending] == [first["id"]]
    assert len(client.get("/change-requests").json()) == 2
    assert client.get(f"/change-requests/{first['id']}").json()["status"] == "pending"
    assert client.get("/change-requests/999").status_code == 404


# --- approval ----------------------------------------------------------------------


def test_approve_create_task_applies_and_audits(client):
    change = submit(client, "create_task", {"project_id": 2, "title": "Deck", "assignee_id": 5})
    response = approve(client, change.json()["id"])
    assert response.status_code == 200
    approved = response.json()
    assert approved["status"] == "approved"
    assert approved["decided_by"] == "reviewer-a"
    assert approved["decided_at"]
    task = approved["result"]
    assert task["id"] == len(TASKS) + 1
    assert task["title"] == "Deck"
    assert client.get("/tasks", params={"project_id": 2}).json()[-1]["id"] == task["id"]
    decision, submitted = audit_events(client, approved["id"])  # newest first
    assert submitted["outcome"] == "pending"
    assert decision["outcome"] == "approved"
    assert decision["actor"] == "reviewer-a"
    assert decision["target"] == f"task {task['id']}"
    assert decision["before"] is None
    assert decision["after"] == task


def test_approve_update_records_before_and_after(client):
    change = submit(client, "update_task", {"status": "in_progress"}, target_id=4).json()
    approved = approve(client, change["id"]).json()
    assert approved["result"]["status"] == "in_progress"
    decision = audit_events(client, change["id"])[0]
    assert decision["before"]["status"] == "todo"
    assert decision["after"]["status"] == "in_progress"
    assert decision["target"] == "task 4"


def test_approve_time_entry_shows_up_in_reports(client):
    before = client.get("/projects/1/hours").json()["logged_hours"]
    change = submit(
        client,
        "create_time_entry",
        {"employee_id": 2, "project_id": 1, "date": "2026-06-10", "hours": 3},
    ).json()
    assert client.get("/projects/1/hours").json()["logged_hours"] == before
    approve(client, change["id"])
    assert client.get("/projects/1/hours").json()["logged_hours"] == before + 3


def test_request_cannot_be_decided_twice(client):
    change = submit(client, "update_task", {"status": "done"}, target_id=4).json()
    assert approve(client, change["id"]).status_code == 200
    again = approve(client, change["id"])
    assert again.status_code == 409
    assert again.json()["detail"] == f"Change request {change['id']} is already approved"
    assert reject(client, change["id"], "too late").status_code == 409


def test_submitter_cannot_review_own_request(client):
    change = submit(client, "update_task", {"status": "done"}, target_id=4).json()
    response = approve(client, change["id"], reviewer="MCP-Agent")
    assert response.status_code == 403
    assert "cannot review it" in response.json()["detail"]
    assert client.get(f"/change-requests/{change['id']}").json()["status"] == "pending"


def test_approve_missing_request_404(client):
    assert approve(client, 999).status_code == 404


def test_blank_reviewer_422(client):
    change = submit(client, "update_task", {"status": "done"}, target_id=4).json()
    assert approve(client, change["id"], reviewer="  ").status_code == 422


# --- staleness -----------------------------------------------------------------------


def test_stale_update_is_refused_with_reason(client):
    first = submit(client, "update_task", {"status": "in_progress"}, target_id=4).json()
    second = submit(client, "update_task", {"status": "done"}, target_id=4).json()
    assert approve(client, first["id"]).status_code == 200
    response = approve(client, second["id"])
    assert response.status_code == 409
    assert "Task 4 changed after this request was submitted" in response.json()["detail"]
    assert "status 'todo' -> 'in_progress'" in response.json()["detail"]
    stale = client.get(f"/change-requests/{second['id']}").json()
    assert stale["status"] == "stale"
    assert stale["decided_by"] == "reviewer-a"
    assert stale["decision_note"] == response.json()["detail"]
    assert task_status(client, 4) == "in_progress"
    event = audit_events(client, second["id"])[0]
    assert event["outcome"] == "stale"
    assert event["before"]["status"] == "todo"
    assert event["after"]["status"] == "in_progress"


def test_direct_edit_after_submission_also_makes_request_stale(client):
    change = submit(client, "update_task", {"status": "done"}, target_id=4).json()
    client.patch("/tasks/4", json={"title": "Document rollback procedure v2"})
    response = approve(client, change["id"])
    assert response.status_code == 409
    assert "title" in response.json()["detail"]


def test_approval_that_cannot_apply_is_marked_failed(client, seeded_engine):
    from sqlmodel import Session

    from platform_api.models import ChangeRequest

    change = submit(client, "create_task", {"project_id": 1, "title": "x"}).json()
    with Session(seeded_engine) as session:  # simulate the project disappearing
        stored = session.get(ChangeRequest, change["id"])
        stored.payload = {"project_id": 999, "title": "x"}
        session.add(stored)
        session.commit()
    response = approve(client, change["id"])
    assert response.status_code == 409
    assert "Project 999 not found" in response.json()["detail"]
    assert client.get(f"/change-requests/{change['id']}").json()["status"] == "failed"
    assert audit_events(client, change["id"])[0]["outcome"] == "failed"
    assert len(client.get("/tasks").json()) == len(TASKS)


# --- rejection -----------------------------------------------------------------------


def test_reject_records_reason_and_changes_nothing(client):
    change = submit(client, "update_task", {"status": "done"}, target_id=4).json()
    response = reject(client, change["id"], "Rollback doc still needs review")
    assert response.status_code == 200
    rejected = response.json()
    assert rejected["status"] == "rejected"
    assert rejected["decision_note"] == "Rollback doc still needs review"
    assert rejected["result"] is None
    assert task_status(client, 4) == "todo"
    event = audit_events(client, change["id"])[0]
    assert event["outcome"] == "rejected"
    assert event["detail"] == "Rollback doc still needs review"


def test_reject_requires_reason(client):
    change = submit(client, "update_task", {"status": "done"}, target_id=4).json()
    response = client.post(
        f"/admin/change-requests/{change['id']}/reject", json={"reviewer": "reviewer-a"}
    )
    assert response.status_code == 422
    assert reject(client, change["id"], "   ").status_code == 422
