"""Platform API tests: happy path plus at least one error case per endpoint group.

Expected numbers are computed from the seed module's literal data rather than
hardcoded, so editing the seed dataset doesn't silently break unrelated tests.
"""

from datetime import date

import pytest

from platform_api.seed import EMPLOYEES, PROJECTS, TASKS, TIME_ENTRIES


def current_iso_week() -> str:
    iso = date.today().isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


# --- seed --------------------------------------------------------------------


def test_seed_counts(client):
    assert len(client.get("/employees").json()) == len(EMPLOYEES) == 8
    assert len(client.get("/projects").json()) == len(PROJECTS) == 5
    assert len(client.get("/tasks").json()) == len(TASKS) == 20
    assert len(TIME_ENTRIES) == 60


def test_seed_is_repeatable(seeded_engine, client):
    from platform_api.seed import seed

    seed(seeded_engine)
    employees = client.get("/employees").json()
    assert len(employees) == 8
    assert employees[0]["name"] == "Ava Chen"


# --- employees ---------------------------------------------------------------


def test_list_employees(client):
    employees = client.get("/employees").json()
    assert {e["name"] for e in employees} == {name for name, *_ in EMPLOYEES}


def test_create_and_get_employee(client):
    response = client.post(
        "/employees",
        json={
            "name": "Iris Vale",
            "role": "Consultant",
            "department": "Strategy",
            "weekly_capacity_hours": 40,
        },
    )
    assert response.status_code == 201
    created = response.json()
    assert created["id"] is not None
    assert client.get(f"/employees/{created['id']}").json()["name"] == "Iris Vale"


def test_get_missing_employee_404(client):
    response = client.get("/employees/9999")
    assert response.status_code == 404
    assert "9999" in response.json()["detail"]


def test_create_employee_invalid_capacity_422(client):
    response = client.post(
        "/employees",
        json={"name": "X", "role": "Y", "department": "Z", "weekly_capacity_hours": -5},
    )
    assert response.status_code == 422


# --- projects ----------------------------------------------------------------


def test_list_and_create_project(client):
    assert len(client.get("/projects").json()) == 5
    response = client.post(
        "/projects",
        json={"name": "New Engagement", "client": "Acme Test Co", "budget_hours": 100},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "active"  # default


def test_create_project_invalid_status_422(client):
    response = client.post(
        "/projects",
        json={"name": "Bad", "client": "C", "status": "paused", "budget_hours": 10},
    )
    assert response.status_code == 422


def test_project_hours_matches_seed(client):
    expected = sum(hours for _, project_id, _, _, hours, _ in TIME_ENTRIES if project_id == 1)
    report = client.get("/projects/1/hours").json()
    assert report["project_name"] == "Orion Data Migration"
    assert report["logged_hours"] == expected
    assert report["remaining_hours"] == report["budget_hours"] - expected


def test_project_hours_missing_project_404(client):
    assert client.get("/projects/9999/hours").status_code == 404


# --- tasks -------------------------------------------------------------------


def test_create_task_defaults_to_todo(client):
    response = client.post("/tasks", json={"project_id": 1, "title": "Try the API"})
    assert response.status_code == 201
    task = response.json()
    assert task["status"] == "todo"
    assert task["assignee_id"] is None


def test_create_task_missing_project_404(client):
    response = client.post("/tasks", json={"project_id": 9999, "title": "Orphan"})
    assert response.status_code == 404


def test_list_tasks_filters(client):
    done = client.get("/tasks", params={"status": "done"}).json()
    assert len(done) == sum(1 for _, _, _, status, _ in TASKS if status == "done")
    project_2 = client.get("/tasks", params={"project_id": 2}).json()
    assert all(t["project_id"] == 2 for t in project_2)
    combined = client.get("/tasks", params={"project_id": 2, "status": "done"}).json()
    assert len(combined) == sum(1 for p, _, _, s, _ in TASKS if p == 2 and s == "done")


def test_patch_task_status(client):
    updated = client.patch("/tasks/1", json={"status": "in_progress"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "in_progress"
    # other fields untouched
    assert updated.json()["title"] == "Map legacy warehouse schema"


def test_patch_task_invalid_status_422(client):
    assert client.patch("/tasks/1", json={"status": "blocked"}).status_code == 422


@pytest.mark.parametrize("field", ["title", "status"])
def test_patch_task_rejects_null_required_fields_without_changes(client, field):
    before = client.get("/tasks").json()
    response = client.patch("/tasks/1", json={field: None, "assignee_id": None})
    assert response.status_code == 422
    assert client.get("/tasks").json() == before


def test_patch_task_can_clear_nullable_fields(client):
    response = client.patch("/tasks/1", json={"assignee_id": None, "due_date": None})
    assert response.status_code == 200
    assert response.json()["assignee_id"] is None
    assert response.json()["due_date"] is None
    assert response.json()["title"] == "Map legacy warehouse schema"


def test_patch_missing_task_404(client):
    assert client.patch("/tasks/9999", json={"status": "done"}).status_code == 404


# --- time entries ------------------------------------------------------------


def test_create_time_entry(client):
    response = client.post(
        "/time-entries",
        json={
            "employee_id": 2,
            "project_id": 1,
            "date": "2026-06-10",
            "hours": 6,
            "note": "API test entry",
        },
    )
    assert response.status_code == 201
    assert response.json()["id"] is not None


def test_create_time_entry_missing_employee_404(client):
    response = client.post(
        "/time-entries",
        json={"employee_id": 9999, "project_id": 1, "date": "2026-06-10", "hours": 6},
    )
    assert response.status_code == 404


def test_create_time_entry_zero_hours_422(client):
    response = client.post(
        "/time-entries",
        json={"employee_id": 2, "project_id": 1, "date": "2026-06-10", "hours": 0},
    )
    assert response.status_code == 422


# --- reports -----------------------------------------------------------------


def test_utilization_current_week(client):
    report = client.get("/reports/utilization", params={"week": current_iso_week()}).json()
    assert report["week"] == current_iso_week()
    assert len(report["employees"]) == 8
    by_name = {row["name"]: row for row in report["employees"]}
    # week_offset == 0 entries are this week's; compare per employee against seed
    expected_marcus = sum(
        hours
        for employee_id, _, week_offset, _, hours, _ in TIME_ENTRIES
        if employee_id == 2 and week_offset == 0
    )
    assert by_name["Marcus Webb"]["logged_hours"] == expected_marcus
    assert by_name["Marcus Webb"]["utilization_pct"] == round(expected_marcus / 40 * 100, 1)


def test_utilization_defaults_to_current_week(client):
    explicit = client.get("/reports/utilization", params={"week": current_iso_week()}).json()
    default = client.get("/reports/utilization").json()
    assert default == explicit


def test_utilization_bad_week_format_422(client):
    response = client.get("/reports/utilization", params={"week": "2026-24"})
    assert response.status_code == 422
    assert "ISO week" in response.json()["detail"]


def test_utilization_invalid_week_number_422(client):
    assert client.get("/reports/utilization", params={"week": "2026-W60"}).status_code == 422
