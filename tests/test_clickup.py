"""ClickUp backend tests against a faked ClickUp API (httpx.MockTransport).

No network: the fake implements just enough of ClickUp v2 (team, spaces, folders,
lists, tasks) to exercise resolution, request shaping, status/due-date mapping, and
error translation. The same canonical task shape must come back regardless of backend.
"""

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from mcp_server import backends, platform_client, server
from mcp_server.backends import ClickUpTaskBackend, PlatformTaskBackend, get_task_backend

DUE_MS = int(datetime(2026, 6, 17, tzinfo=UTC).timestamp() * 1000)


def cu_task(task_id, name, status, cu_list, assignees, due_ms=None, team_id="9"):
    return {
        "id": task_id,
        "name": name,
        "status": {"status": status},
        "list": cu_list,
        "assignees": assignees,
        "due_date": str(due_ms) if due_ms else None,
        "team_id": team_id,
        "url": f"https://app.clickup.com/t/{task_id}",
    }


class FakeClickUp:
    """In-memory stand-in for api.clickup.com/api/v2."""

    def __init__(self):
        self.orion = {"id": "901", "name": "Orion Data Migration"}
        self.atlas = {"id": "902", "name": "Atlas KPI Dashboard"}
        self.marcus = {"id": 11, "username": "Marcus Webb"}
        self.tessa = {"id": 12, "username": "Tessa Morgan"}
        members = [{"user": self.marcus}, {"user": self.tessa}]
        self.teams = [{"id": "9", "name": "Acme Workspace", "members": members}]
        self.include_last_page = True
        self.last_page_override = None
        self.repeat_task_pages = False
        self.tasks = {
            "abc123": cu_task(
                "abc123", "Build pipeline", "in progress", self.orion, [self.marcus], DUE_MS
            ),
            "def456": cu_task("def456", "Wireframe dashboard", "to do", self.atlas, [self.tessa]),
        }
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("Authorization") != "test-token":
            return httpx.Response(401, json={"err": "Token invalid", "ECODE": "OAUTH_025"})
        path = request.url.path.removeprefix("/api/v2")
        params = request.url.params
        if path == "/team":
            return httpx.Response(200, json={"teams": self.teams})
        if path == "/team/9/space":
            return httpx.Response(200, json={"spaces": [{"id": "100", "name": "Client Work"}]})
        if path == "/space/100/folder":
            folder = {"id": "200", "name": "Engagements", "lists": [self.orion]}
            return httpx.Response(200, json={"folders": [folder]})
        if path == "/space/100/list":
            return httpx.Response(200, json={"lists": [self.atlas]})
        if path == "/team/9/task":
            tasks = list(self.tasks.values())
            if params.get("include_closed") != "true":
                tasks = [
                    task for task in tasks if task["status"]["status"] not in {"closed", "complete"}
                ]
            if "list_ids[]" in params:
                tasks = [t for t in tasks if t["list"]["id"] == params["list_ids[]"]]
            if "assignees[]" in params:
                tasks = [
                    t
                    for t in tasks
                    if any(str(a["id"]) == params["assignees[]"] for a in t["assignees"])
                ]
            if "statuses[]" in params:
                tasks = [t for t in tasks if t["status"]["status"] == params["statuses[]"]]
            page = int(params.get("page", "0"))
            result_page = 0 if self.repeat_task_pages else page
            page_tasks = tasks[result_page * 100 : (result_page + 1) * 100]
            payload = {"tasks": page_tasks}
            if self.include_last_page:
                payload["last_page"] = (
                    self.last_page_override
                    if self.last_page_override is not None
                    else (page + 1) * 100 >= len(tasks)
                )
            return httpx.Response(200, json=payload)
        if path.startswith("/task/") and request.method == "GET":
            task_id = path.split("/")[2]
            if task_id not in self.tasks:
                return httpx.Response(404, json={"err": "Task not found", "ECODE": "ITEM_013"})
            return httpx.Response(200, json=self.tasks[task_id])
        if path.startswith("/list/") and path.endswith("/task") and request.method == "POST":
            list_id = path.split("/")[2]
            cu_list = next(li for li in (self.orion, self.atlas) if li["id"] == list_id)
            body = json.loads(request.content)
            assignees = [
                user
                for user in (self.marcus, self.tessa)
                if user["id"] in body.get("assignees", [])
            ]
            task = cu_task(
                "new001", body["name"], "to do", cu_list, assignees, body.get("due_date")
            )
            self.tasks[task["id"]] = task
            return httpx.Response(200, json=task)
        if path.startswith("/task/") and request.method == "PUT":
            task_id = path.split("/")[2]
            if task_id not in self.tasks:
                return httpx.Response(404, json={"err": "Task not found", "ECODE": "ITEM_013"})
            body = json.loads(request.content)
            self.tasks[task_id]["status"] = {"status": body["status"]}
            return httpx.Response(200, json=self.tasks[task_id])
        return httpx.Response(404, json={"err": f"no fake route for {path}"})


def run(coro):
    return asyncio.run(coro)


class AuditSink:
    """Stands in for the platform's POST /audit-events so no test reaches a real server."""

    def __init__(self):
        self.reports: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/audit-events"
        self.reports.append(json.loads(request.content))
        return httpx.Response(201, json={})


@pytest.fixture
def audit_sink(monkeypatch):
    sink = AuditSink()
    monkeypatch.setattr(platform_client, "_transport", httpx.MockTransport(sink.handler))
    return sink


@pytest.fixture
def clickup(monkeypatch, audit_sink):
    fake = FakeClickUp()
    monkeypatch.setattr(backends, "_cu_transport", httpx.MockTransport(fake.handler))
    monkeypatch.setenv("CLICKUP_API_TOKEN", "test-token")
    monkeypatch.delenv("CLICKUP_TEAM_ID", raising=False)
    monkeypatch.delenv("CLICKUP_WRITES_ENABLED", raising=False)
    # ClickUp writes need approval mode off; the tests below cover the ClickUp gates.
    monkeypatch.setenv("OPS_REQUIRE_APPROVAL", "false")
    return fake


@pytest.fixture
def backend(clickup):
    return ClickUpTaskBackend()


# --- backend selection -----------------------------------------------------------


def test_backend_defaults_to_platform(monkeypatch):
    monkeypatch.delenv("OPS_TASK_BACKEND", raising=False)
    assert isinstance(get_task_backend(), PlatformTaskBackend)


def test_backend_env_selects_clickup(monkeypatch):
    monkeypatch.setenv("OPS_TASK_BACKEND", "clickup")
    assert isinstance(get_task_backend(), ClickUpTaskBackend)


def test_backend_unknown_value_errors(monkeypatch):
    monkeypatch.setenv("OPS_TASK_BACKEND", "jira")
    with pytest.raises(ValueError, match="Unknown OPS_TASK_BACKEND 'jira'"):
        get_task_backend()


# --- reads -------------------------------------------------------------------------


def test_list_tasks_normalizes_to_canonical_shape(backend):
    tasks = run(backend.list_tasks(project=None, assignee=None, status=None))
    by_id = {t["id"]: t for t in tasks}
    orion_task = by_id["abc123"]
    assert orion_task["title"] == "Build pipeline"
    assert orion_task["status"] == "in_progress"  # "in progress" mapped back
    assert orion_task["project_name"] == "Orion Data Migration"
    assert orion_task["assignee_name"] == "Marcus Webb"
    assert orion_task["due_date"] == "2026-06-17"  # ms epoch converted
    assert by_id["def456"]["status"] == "todo"
    assert by_id["def456"]["due_date"] is None


def test_reads_remain_enabled_when_write_gate_is_closed(backend, monkeypatch):
    monkeypatch.setenv("CLICKUP_WRITES_ENABLED", "false")
    tasks = run(backend.list_tasks(project=None, assignee=None, status=None))
    assert {task["id"] for task in tasks} == {"abc123", "def456"}


def test_list_tasks_falls_back_to_page_size_without_last_page(backend, clickup):
    clickup.include_last_page = False
    clickup.tasks = {
        f"bulk{i:03d}": cu_task(
            f"bulk{i:03d}", f"Task {i}", "to do", clickup.atlas, [clickup.tessa]
        )
        for i in range(101)
    }

    tasks = run(backend.list_tasks(project=None, assignee=None, status=None))

    assert len(tasks) == 101
    task_requests = [r for r in clickup.requests if r.url.path.endswith("/team/9/task")]
    assert [request.url.params["page"] for request in task_requests] == ["0", "1"]


def test_list_tasks_honors_last_page_on_a_full_page(backend, clickup):
    clickup.tasks = {
        f"bulk{i:03d}": cu_task(
            f"bulk{i:03d}", f"Task {i}", "to do", clickup.atlas, [clickup.tessa]
        )
        for i in range(100)
    }

    tasks = run(backend.list_tasks(project=None, assignee=None, status=None))

    assert len(tasks) == 100
    task_requests = [r for r in clickup.requests if r.url.path.endswith("/team/9/task")]
    assert [request.url.params["page"] for request in task_requests] == ["0"]


def test_list_tasks_explicitly_includes_closed_tasks(backend, clickup):
    clickup.tasks["def456"]["status"] = {"status": "complete"}

    tasks = run(backend.list_tasks(project=None, assignee=None, status=None))

    assert {task["id"] for task in tasks} == {"abc123", "def456"}
    task_request = next(r for r in clickup.requests if r.url.path.endswith("/team/9/task"))
    assert task_request.url.params["include_closed"] == "true"


def test_list_tasks_stops_on_a_repeated_page(backend, clickup):
    clickup.tasks = {
        f"bulk{i:03d}": cu_task(
            f"bulk{i:03d}", f"Task {i}", "to do", clickup.atlas, [clickup.tessa]
        )
        for i in range(100)
    }
    clickup.repeat_task_pages = True
    clickup.last_page_override = False

    with pytest.raises(ValueError, match="repeated task page.*stopped safely"):
        run(backend.list_tasks(project=None, assignee=None, status=None))


def test_list_tasks_has_a_max_page_guard(backend, clickup, monkeypatch):
    monkeypatch.setattr(backends, "CLICKUP_MAX_TASK_PAGES", 2)
    clickup.tasks = {
        f"bulk{i:03d}": cu_task(
            f"bulk{i:03d}", f"Task {i}", "to do", clickup.atlas, [clickup.tessa]
        )
        for i in range(300)
    }
    clickup.last_page_override = False

    with pytest.raises(ValueError, match="pagination exceeded 2 pages"):
        run(backend.list_tasks(project=None, assignee=None, status=None))


def test_list_tasks_rejects_invalid_last_page(backend, clickup):
    clickup.last_page_override = "yes"
    with pytest.raises(ValueError, match="invalid 'last_page' value"):
        run(backend.list_tasks(project=None, assignee=None, status=None))


def test_list_tasks_project_filter_resolves_list_across_folders(backend):
    # Orion lives inside a folder, Atlas is folderless — both must be findable
    tasks = run(backend.list_tasks(project="orion", assignee=None, status=None))
    assert [t["id"] for t in tasks] == ["abc123"]
    tasks = run(backend.list_tasks(project="atlas", assignee=None, status=None))
    assert [t["id"] for t in tasks] == ["def456"]


def test_list_tasks_assignee_and_status_filters(backend, clickup):
    tasks = run(backend.list_tasks(project=None, assignee="tessa", status="todo"))
    assert [t["id"] for t in tasks] == ["def456"]
    task_request = next(r for r in clickup.requests if r.url.path.endswith("/team/9/task"))
    assert task_request.url.params["statuses[]"] == "to do"  # canonical → ClickUp


def test_ambiguous_list_name_lists_candidates(backend):
    with pytest.raises(ValueError, match="Ambiguous project \\(ClickUp list\\) 'a'"):
        run(backend.list_tasks(project="a", assignee=None, status=None))


def test_multiple_workspaces_require_explicit_team_id(backend, clickup):
    clickup.teams.append({"id": "10", "name": "Second Workspace", "members": []})
    with pytest.raises(ValueError, match="multiple workspaces.*Set CLICKUP_TEAM_ID"):
        run(backend.list_tasks(project=None, assignee=None, status=None))


def test_success_response_must_be_json(clickup, monkeypatch):
    def non_json_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="sensitive upstream response", request=request)

    monkeypatch.setattr(backends, "_cu_transport", httpx.MockTransport(non_json_response))
    with pytest.raises(ValueError, match="non-JSON success response") as error:
        run(ClickUpTaskBackend().list_tasks(project=None, assignee=None, status=None))
    assert "sensitive upstream response" not in str(error.value)


def test_workspace_response_requires_teams_list(backend, clickup):
    clickup.teams = None
    with pytest.raises(ValueError, match="workspace response is missing a valid 'teams' list"):
        run(backend.list_tasks(project=None, assignee=None, status=None))


def test_task_listing_response_requires_tasks_list(clickup, monkeypatch):
    def missing_tasks(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v2")
        if path == "/team":
            return httpx.Response(200, json={"teams": clickup.teams})
        return httpx.Response(200, json={"last_page": True})

    monkeypatch.setattr(backends, "_cu_transport", httpx.MockTransport(missing_tasks))
    with pytest.raises(ValueError, match="task listing response is missing a valid 'tasks' list"):
        run(ClickUpTaskBackend().list_tasks(project=None, assignee=None, status=None))


# --- writes ------------------------------------------------------------------------


def test_writes_are_disabled_by_default(backend, clickup):
    with pytest.raises(ValueError, match="ClickUp writes are disabled by default"):
        run(
            backend.create_task(project="Atlas", title="Unsafe write", assignee=None, due_date=None)
        )
    with pytest.raises(ValueError, match="ClickUp writes are disabled by default"):
        run(backend.update_task_status(task_id="abc123", status="done"))
    assert clickup.requests == []


def test_writes_are_blocked_while_approval_mode_is_on(backend, clickup, monkeypatch):
    monkeypatch.delenv("OPS_REQUIRE_APPROVAL")
    monkeypatch.setenv("CLICKUP_WRITES_ENABLED", "true")
    with pytest.raises(ValueError, match="Approval mode is on.*OPS_REQUIRE_APPROVAL=false"):
        run(backend.create_task(project="Atlas", title="Queued?", assignee=None, due_date=None))
    with pytest.raises(ValueError, match="Approval mode is on"):
        run(backend.update_task_status(task_id="abc123", status="done"))
    assert clickup.requests == []


def test_blocked_write_through_the_tool_is_audited_as_error(clickup, audit_sink, monkeypatch):
    monkeypatch.setenv("OPS_TASK_BACKEND", "clickup")
    monkeypatch.delenv("OPS_REQUIRE_APPROVAL")
    with pytest.raises(ValueError, match="Approval mode is on"):
        run(server.update_task_status(task_id="abc123", status="done"))
    [report] = audit_sink.reports
    assert report["outcome"] == "error"
    assert report["tool"] == "update_task_status"
    assert report["arguments"] == {"task_id": "abc123", "status": "done"}


def test_successful_writes_are_reported_as_external(backend, clickup, audit_sink, monkeypatch):
    monkeypatch.setenv("CLICKUP_WRITES_ENABLED", "true")
    task = run(backend.create_task(project="Atlas", title="Deck", assignee=None, due_date=None))
    run(backend.update_task_status(task_id="abc123", status="done"))
    created, updated = audit_sink.reports
    assert created["tool"] == "create_task"
    assert created["outcome"] == "external"
    assert created["result"] == task
    assert updated["tool"] == "update_task_status"
    assert updated["result"]["status"] == "done"


def test_create_task_maps_payload(backend, clickup, monkeypatch):
    monkeypatch.setenv("CLICKUP_WRITES_ENABLED", "true")
    task = run(
        backend.create_task(
            project="Atlas", title="New deck", assignee="Marcus", due_date="2026-07-01"
        )
    )
    assert task["id"] == "new001"
    assert task["status"] == "todo"
    assert task["project_name"] == "Atlas KPI Dashboard"
    assert task["assignee_name"] == "Marcus Webb"
    create = next(r for r in clickup.requests if r.method == "POST")
    body = json.loads(create.content)
    assert body["assignees"] == [11]
    assert body["due_date"] == int(datetime(2026, 7, 1, tzinfo=UTC).timestamp() * 1000)


def test_malformed_create_response_warns_against_retry(backend, clickup, monkeypatch):
    monkeypatch.setenv("CLICKUP_WRITES_ENABLED", "true")

    def malformed_after_write(request: httpx.Request) -> httpx.Response:
        response = clickup.handler(request)
        if request.method == "POST":
            return httpx.Response(200, json={"unexpected": True}, request=request)
        return response

    monkeypatch.setattr(backends, "_cu_transport", httpx.MockTransport(malformed_after_write))
    with pytest.raises(
        ValueError, match="accepted the task creation.*may have succeeded.*before retrying"
    ):
        run(backend.create_task(project="Atlas", title="New deck", assignee=None, due_date=None))

    assert "new001" in clickup.tasks
    assert len([request for request in clickup.requests if request.method == "POST"]) == 1


def test_non_json_create_response_warns_against_retry(backend, clickup, monkeypatch):
    monkeypatch.setenv("CLICKUP_WRITES_ENABLED", "true")

    def non_json_after_write(request: httpx.Request) -> httpx.Response:
        response = clickup.handler(request)
        if request.method == "POST":
            return httpx.Response(200, text="sensitive upstream response", request=request)
        return response

    monkeypatch.setattr(backends, "_cu_transport", httpx.MockTransport(non_json_after_write))
    with pytest.raises(
        ValueError, match="POST request.*may have succeeded.*before retrying"
    ) as error:
        run(backend.create_task(project="Atlas", title="New deck", assignee=None, due_date=None))

    assert "sensitive upstream response" not in str(error.value)
    assert "new001" in clickup.tasks
    assert len([request for request in clickup.requests if request.method == "POST"]) == 1


def test_create_timeout_after_send_warns_against_retry(backend, clickup, monkeypatch):
    monkeypatch.setenv("CLICKUP_WRITES_ENABLED", "true")

    def timeout_after_write(request: httpx.Request) -> httpx.Response:
        response = clickup.handler(request)
        if request.method == "POST":
            raise httpx.ReadTimeout("sensitive timeout detail", request=request)
        return response

    monkeypatch.setattr(backends, "_cu_transport", httpx.MockTransport(timeout_after_write))
    with pytest.raises(
        ValueError, match="POST request.*may have succeeded.*before retrying"
    ) as error:
        run(backend.create_task(project="Atlas", title="New deck", assignee=None, due_date=None))

    assert "sensitive timeout detail" not in str(error.value)
    assert "new001" in clickup.tasks
    assert len([request for request in clickup.requests if request.method == "POST"]) == 1


def test_update_task_status_maps_done_to_complete(backend, clickup, monkeypatch):
    monkeypatch.setenv("CLICKUP_WRITES_ENABLED", "true")
    monkeypatch.setenv("CLICKUP_TEAM_ID", "9")
    clickup.teams.append({"id": "10", "name": "Second Workspace", "members": []})
    task = run(backend.update_task_status(task_id="abc123", status="done"))
    assert task["status"] == "done"
    update = next(r for r in clickup.requests if r.method == "PUT")
    assert json.loads(update.content) == {"status": "complete"}


def test_malformed_update_response_warns_against_retry(backend, clickup, monkeypatch):
    monkeypatch.setenv("CLICKUP_WRITES_ENABLED", "true")

    def malformed_after_write(request: httpx.Request) -> httpx.Response:
        response = clickup.handler(request)
        if request.method == "PUT":
            return httpx.Response(200, json={"unexpected": True}, request=request)
        return response

    monkeypatch.setattr(backends, "_cu_transport", httpx.MockTransport(malformed_after_write))
    with pytest.raises(
        ValueError, match="accepted the status update.*may have succeeded.*before retrying"
    ):
        run(backend.update_task_status(task_id="abc123", status="done"))

    assert clickup.tasks["abc123"]["status"]["status"] == "complete"
    assert len([request for request in clickup.requests if request.method == "PUT"]) == 1


def test_non_object_update_response_warns_against_retry(backend, clickup, monkeypatch):
    monkeypatch.setenv("CLICKUP_WRITES_ENABLED", "true")

    def list_after_write(request: httpx.Request) -> httpx.Response:
        response = clickup.handler(request)
        if request.method == "PUT":
            return httpx.Response(200, json=[{"sensitive": "payload"}], request=request)
        return response

    monkeypatch.setattr(backends, "_cu_transport", httpx.MockTransport(list_after_write))
    with pytest.raises(
        ValueError, match="PUT request.*may have succeeded.*before retrying"
    ) as error:
        run(backend.update_task_status(task_id="abc123", status="done"))

    assert "sensitive" not in str(error.value)
    assert clickup.tasks["abc123"]["status"]["status"] == "complete"
    assert len([request for request in clickup.requests if request.method == "PUT"]) == 1


def test_update_timeout_after_send_warns_against_retry(backend, clickup, monkeypatch):
    monkeypatch.setenv("CLICKUP_WRITES_ENABLED", "true")

    def timeout_after_write(request: httpx.Request) -> httpx.Response:
        response = clickup.handler(request)
        if request.method == "PUT":
            raise httpx.ReadTimeout("sensitive timeout detail", request=request)
        return response

    monkeypatch.setattr(backends, "_cu_transport", httpx.MockTransport(timeout_after_write))
    with pytest.raises(
        ValueError, match="PUT request.*may have succeeded.*before retrying"
    ) as error:
        run(backend.update_task_status(task_id="abc123", status="done"))

    assert "sensitive timeout detail" not in str(error.value)
    assert clickup.tasks["abc123"]["status"]["status"] == "complete"
    assert len([request for request in clickup.requests if request.method == "PUT"]) == 1


def test_update_requires_team_id_for_multiple_workspaces(backend, clickup, monkeypatch):
    monkeypatch.setenv("CLICKUP_WRITES_ENABLED", "true")
    clickup.teams.append({"id": "10", "name": "Second Workspace", "members": []})

    with pytest.raises(ValueError, match="multiple workspaces.*Set CLICKUP_TEAM_ID"):
        run(backend.update_task_status(task_id="abc123", status="done"))
    assert not any(request.method == "PUT" for request in clickup.requests)


def test_update_rejects_task_from_another_workspace(backend, clickup, monkeypatch):
    monkeypatch.setenv("CLICKUP_WRITES_ENABLED", "true")
    monkeypatch.setenv("CLICKUP_TEAM_ID", "9")
    clickup.tasks["abc123"]["team_id"] = "10"

    with pytest.raises(ValueError, match="belongs to workspace 10.*selected workspace 9"):
        run(backend.update_task_status(task_id="abc123", status="done"))
    assert not any(request.method == "PUT" for request in clickup.requests)


def test_update_rejects_task_with_unverifiable_workspace(backend, clickup, monkeypatch):
    monkeypatch.setenv("CLICKUP_WRITES_ENABLED", "true")
    del clickup.tasks["abc123"]["team_id"]

    with pytest.raises(ValueError, match="task response has no team_id"):
        run(backend.update_task_status(task_id="abc123", status="done"))
    assert not any(request.method == "PUT" for request in clickup.requests)


def test_update_missing_task_surfaces_clickup_error(backend, monkeypatch):
    monkeypatch.setenv("CLICKUP_WRITES_ENABLED", "true")
    with pytest.raises(ValueError, match="ClickUp API error 404"):
        run(backend.update_task_status(task_id="nope", status="done"))


# --- auth & wiring -------------------------------------------------------------------


def test_missing_token_is_actionable(clickup, monkeypatch):
    monkeypatch.delenv("CLICKUP_API_TOKEN")
    with pytest.raises(ValueError, match="CLICKUP_API_TOKEN is not set"):
        run(ClickUpTaskBackend().list_tasks(project=None, assignee=None, status=None))


def test_rejected_token_is_actionable(clickup, monkeypatch):
    monkeypatch.setenv("CLICKUP_API_TOKEN", "wrong")
    with pytest.raises(ValueError, match="check CLICKUP_API_TOKEN"):
        run(ClickUpTaskBackend().list_tasks(project=None, assignee=None, status=None))


def test_timeout_is_actionable(clickup, monkeypatch):
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    monkeypatch.setattr(backends, "_cu_transport", httpx.MockTransport(timeout))
    with pytest.raises(ValueError, match="timed out after 15 seconds.*retry"):
        run(ClickUpTaskBackend().list_tasks(project=None, assignee=None, status=None))


def test_request_error_is_actionable(clickup, monkeypatch):
    def connection_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated connection failure", request=request)

    monkeypatch.setattr(backends, "_cu_transport", httpx.MockTransport(connection_failure))
    with pytest.raises(ValueError, match="Cannot reach ClickUp.*retry"):
        run(ClickUpTaskBackend().list_tasks(project=None, assignee=None, status=None))


def test_server_tools_route_through_clickup_backend(clickup, monkeypatch):
    monkeypatch.setenv("OPS_TASK_BACKEND", "clickup")
    monkeypatch.setenv("CLICKUP_WRITES_ENABLED", "true")
    tasks = run(server.list_tasks())
    assert {t["id"] for t in tasks} == {"abc123", "def456"}
    updated = run(server.update_task_status(task_id="def456", status="in_progress"))
    assert updated["status"] == "in_progress"
