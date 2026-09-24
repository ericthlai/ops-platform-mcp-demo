"""Task-tool backends: the same three task tools (list_tasks, create_task,
update_task_status) served by either the mock platform or the real ClickUp API.

Select with OPS_TASK_BACKEND=platform (default) or clickup. The non-task tools
(employees, hours, utilization, time logging) are always platform-backed — adapter
scope is the task tools only.

ClickUp mapping: a "project" is a ClickUp **list** (looked up by name across every
space and folder in the workspace), an "assignee" is a workspace member (matched on
username), and the canonical statuses todo / in_progress / done translate to ClickUp's
"to do" / "in progress" / "complete". ClickUp task ids are alphanumeric strings.
Requires CLICKUP_API_TOKEN; CLICKUP_TEAM_ID is optional when the token sees exactly
one workspace.

Approval: platform writes are queued as change requests while approval mode is on (the
default; see approvals.py). The approval queue can only execute platform changes, so
ClickUp writes are blocked in approval mode and need two explicit opt-ins:
OPS_REQUIRE_APPROVAL=false and CLICKUP_WRITES_ENABLED=true. Successful ClickUp writes are
then reported to the platform audit log as external writes.
"""

import os
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from . import platform_client
from .approvals import approval_required, submit_write
from .audit import report_tool_call
from .resolution import pick_by_name


class TaskBackend(Protocol):
    async def list_tasks(
        self, project: str | None, assignee: str | None, status: str | None
    ) -> list[dict]: ...

    async def create_task(
        self, project: str, title: str, assignee: str | None, due_date: str | None
    ) -> dict: ...

    async def update_task_status(self, task_id: str, status: str) -> dict: ...


# --- platform ------------------------------------------------------------------


class PlatformTaskBackend:
    """Tasks served by the mock platform's REST API (ints for ids, enriched names)."""

    async def list_tasks(
        self, project: str | None, assignee: str | None, status: str | None
    ) -> list[dict]:
        params: dict[str, Any] = {}
        if project is not None:
            params["project_id"] = (await platform_client.resolve_project(project))["id"]
        if assignee is not None:
            params["assignee_id"] = (await platform_client.resolve_employee(assignee))["id"]
        if status is not None:
            params["status"] = status
        tasks = await platform_client.request("GET", "/tasks", params=params)
        project_names = {
            p["id"]: p["name"] for p in await platform_client.request("GET", "/projects")
        }
        employee_names = {
            e["id"]: e["name"] for e in await platform_client.request("GET", "/employees")
        }
        for task in tasks:
            task["project_name"] = project_names.get(task["project_id"])
            task["assignee_name"] = employee_names.get(task["assignee_id"])
        return tasks

    async def create_task(
        self, project: str, title: str, assignee: str | None, due_date: str | None
    ) -> dict:
        payload: dict[str, Any] = {
            "project_id": (await platform_client.resolve_project(project))["id"],
            "title": title,
        }
        if assignee is not None:
            payload["assignee_id"] = (await platform_client.resolve_employee(assignee))["id"]
        if due_date is not None:
            payload["due_date"] = due_date
        arguments = {"project": project, "title": title, "assignee": assignee, "due_date": due_date}
        return await submit_write("create_task", "create_task", payload, arguments)

    async def update_task_status(self, task_id: str, status: str) -> dict:
        arguments = {"task_id": task_id, "status": status}
        return await submit_write(
            "update_task_status", "update_task", {"status": status}, arguments, target_id=task_id
        )


# --- clickup ---------------------------------------------------------------------

CLICKUP_URL = "https://api.clickup.com/api/v2"
CLICKUP_MAX_TASK_PAGES = 1000
CLICKUP_REQUEST_TIMEOUT_SECONDS = 15.0
CLICKUP_TASK_PAGE_SIZE = 100

# Tests inject an httpx.MockTransport here to fake the ClickUp API.
_cu_transport: httpx.AsyncBaseTransport | None = None

CANONICAL_TO_CLICKUP = {"todo": "to do", "in_progress": "in progress", "done": "complete"}
CLICKUP_TO_CANONICAL = {
    "to do": "todo",
    "open": "todo",
    "in progress": "in_progress",
    "complete": "done",
    "closed": "done",
}


class ClickUpTaskBackend:
    """Tasks served by the real ClickUp API (string ids, workspace lists as projects)."""

    def _require_writes_enabled(self) -> None:
        if approval_required():
            raise ValueError(
                "Approval mode is on, and change requests can only apply platform changes, so "
                "ClickUp writes are blocked. To write to ClickUp directly, set "
                "OPS_REQUIRE_APPROVAL=false and CLICKUP_WRITES_ENABLED=true."
            )
        if os.environ.get("CLICKUP_WRITES_ENABLED", "").strip().lower() != "true":
            raise ValueError(
                "ClickUp writes are disabled by default. Set CLICKUP_WRITES_ENABLED=true "
                "only after confirming the target workspace and intended change."
            )

    @staticmethod
    def _ambiguous_write_error(method: str, detail: str) -> ValueError:
        """Return a retry-safe error when ClickUp may have accepted a mutation."""
        return ValueError(
            f"ClickUp did not provide a reliable confirmation for the {method} request "
            f"({detail}). The write may have succeeded; verify it in ClickUp before retrying."
        )

    async def _cu(self, method: str, path: str, **kwargs: Any) -> Any:
        method = method.upper()
        is_write = method not in {"GET", "HEAD", "OPTIONS"}
        if is_write:
            self._require_writes_enabled()
        token = os.environ.get("CLICKUP_API_TOKEN")
        if not token:
            raise ValueError(
                "CLICKUP_API_TOKEN is not set. Create a personal API token in ClickUp "
                "(Settings → Apps) and export it before using the clickup backend."
            )
        try:
            async with httpx.AsyncClient(
                base_url=CLICKUP_URL,
                transport=_cu_transport,
                timeout=CLICKUP_REQUEST_TIMEOUT_SECONDS,
                headers={"Authorization": token},
            ) as client:
                response = await client.request(method, path, **kwargs)
        except httpx.TimeoutException:
            if is_write:
                raise self._ambiguous_write_error(
                    method, f"timed out after {CLICKUP_REQUEST_TIMEOUT_SECONDS:g} seconds"
                ) from None
            raise ValueError(
                f"ClickUp request timed out after {CLICKUP_REQUEST_TIMEOUT_SECONDS:g} seconds — "
                "check network connectivity and retry."
            ) from None
        except httpx.RequestError:
            if is_write:
                raise self._ambiguous_write_error(method, "the connection failed") from None
            raise ValueError(
                f"Cannot reach ClickUp at {CLICKUP_URL} — check network access and retry."
            ) from None
        if response.status_code in (401, 403):
            raise ValueError(
                f"ClickUp rejected the request (HTTP {response.status_code}) — "
                "check CLICKUP_API_TOKEN."
            )
        if response.status_code >= 400:
            try:
                err = response.json().get("err", response.text)
            except ValueError:
                err = response.text
            raise ValueError(f"ClickUp API error {response.status_code}: {err}")
        try:
            data = response.json()
        except ValueError:
            if is_write:
                raise self._ambiguous_write_error(
                    method, "the success response was not valid JSON"
                ) from None
            raise ValueError(
                "ClickUp returned a non-JSON success response — retry, then check API "
                "compatibility if the problem persists."
            ) from None
        if not isinstance(data, dict):
            if is_write:
                raise self._ambiguous_write_error(
                    method, "the success response had an unexpected shape"
                )
            raise ValueError(
                "ClickUp returned an unexpected success payload — retry, then check API "
                "compatibility if the problem persists."
            )
        return data

    @staticmethod
    def _required_list(data: dict, key: str, context: str) -> list:
        value = data.get(key)
        if not isinstance(value, list):
            raise ValueError(
                f"ClickUp {context} response is missing a valid {key!r} list — "
                "the request stopped safely."
            )
        return value

    async def _team(self) -> dict:
        teams = self._required_list(await self._cu("GET", "/team"), "teams", "workspace")
        if not teams:
            raise ValueError("This ClickUp token has no workspaces.")
        if any(
            not isinstance(team, dict)
            or team.get("id") is None
            or not isinstance(team.get("name"), str)
            for team in teams
        ):
            raise ValueError(
                "ClickUp workspace response contains an invalid workspace entry — "
                "no change was sent."
            )
        team_id = (os.environ.get("CLICKUP_TEAM_ID") or "").strip()
        if not team_id and len(teams) > 1:
            known = ", ".join(f"{t['name']} (id {t['id']})" for t in teams)
            raise ValueError(
                "This ClickUp token can access multiple workspaces. Set CLICKUP_TEAM_ID "
                f"explicitly before continuing. Workspaces: {known}"
            )
        if not team_id:
            return teams[0]
        for team in teams:
            if str(team["id"]) == str(team_id):
                return team
        known = ", ".join(f"{t['name']} (id {t['id']})" for t in teams)
        raise ValueError(f"CLICKUP_TEAM_ID {team_id!r} not found. Workspaces: {known}")

    async def _resolve_list(self, project: str) -> dict:
        team = await self._team()
        lists: list[dict] = []
        spaces = (await self._cu("GET", f"/team/{team['id']}/space"))["spaces"]
        for space in spaces:
            for folder in (await self._cu("GET", f"/space/{space['id']}/folder"))["folders"]:
                lists.extend(folder.get("lists", []))
            lists.extend((await self._cu("GET", f"/space/{space['id']}/list"))["lists"])
        candidates = [{"id": item["id"], "name": item["name"]} for item in lists]
        return pick_by_name("project (ClickUp list)", candidates, project)

    async def _resolve_member(self, assignee: str) -> dict:
        team = await self._team()
        members = [
            {"id": member["user"]["id"], "name": member["user"]["username"]}
            for member in team.get("members", [])
        ]
        return pick_by_name("assignee (ClickUp member)", members, assignee)

    def _normalize(self, task: dict) -> dict:
        raw_status = ((task.get("status") or {}).get("status") or "").lower()
        assignees = task.get("assignees") or []
        due_ms = task.get("due_date")
        due_date = (
            datetime.fromtimestamp(int(due_ms) / 1000, tz=UTC).date().isoformat()
            if due_ms
            else None
        )
        return {
            "id": task["id"],
            "title": task.get("name"),
            "status": CLICKUP_TO_CANONICAL.get(raw_status, raw_status),
            "project_name": (task.get("list") or {}).get("name"),
            "assignee_name": assignees[0]["username"] if assignees else None,
            "due_date": due_date,
            "url": task.get("url"),
        }

    def _normalize_write_result(self, task: dict, operation: str) -> dict:
        """Normalize a successful write without making an unsafe retry look harmless."""
        try:
            return self._normalize(task)
        except (AttributeError, IndexError, KeyError, OverflowError, TypeError, ValueError):
            raise ValueError(
                f"ClickUp accepted the {operation}, but returned an unexpected task payload. "
                "The write may have succeeded; verify it in ClickUp before retrying."
            ) from None

    async def list_tasks(
        self, project: str | None, assignee: str | None, status: str | None
    ) -> list[dict]:
        team = await self._team()
        params: list[tuple[str, str]] = [("include_closed", "true")]
        if project is not None:
            params.append(("list_ids[]", str((await self._resolve_list(project))["id"])))
        if assignee is not None:
            params.append(("assignees[]", str((await self._resolve_member(assignee))["id"])))
        if status is not None:
            params.append(("statuses[]", CANONICAL_TO_CLICKUP.get(status, status)))
        tasks: list[dict] = []
        seen_pages: set[tuple[str, ...]] = set()
        for page in range(CLICKUP_MAX_TASK_PAGES):
            page_params = [*params, ("page", str(page))]
            data = await self._cu("GET", f"/team/{team['id']}/task", params=page_params)
            batch = self._required_list(data, "tasks", "task listing")
            if any(not isinstance(task, dict) or task.get("id") is None for task in batch):
                raise ValueError(
                    "ClickUp task listing contains an invalid task entry — no change was sent."
                )
            last_page = data.get("last_page")
            if last_page is not None and not isinstance(last_page, bool):
                raise ValueError(
                    "ClickUp task listing returned an invalid 'last_page' value — "
                    "pagination stopped safely."
                )
            page_signature = tuple(str(task["id"]) for task in batch)
            if page_signature and page_signature in seen_pages:
                raise ValueError(
                    "ClickUp returned a repeated task page — pagination stopped safely."
                )
            seen_pages.add(page_signature)
            tasks.extend(batch)
            if last_page is True or not batch:
                break
            if last_page is None and len(batch) < CLICKUP_TASK_PAGE_SIZE:
                break
        else:
            raise ValueError(
                f"ClickUp task pagination exceeded {CLICKUP_MAX_TASK_PAGES} pages — "
                "narrow the filters and retry."
            )
        return [self._normalize(task) for task in tasks]

    async def create_task(
        self, project: str, title: str, assignee: str | None, due_date: str | None
    ) -> dict:
        self._require_writes_enabled()
        cu_list = await self._resolve_list(project)
        payload: dict[str, Any] = {"name": title}
        if assignee is not None:
            payload["assignees"] = [(await self._resolve_member(assignee))["id"]]
        if due_date is not None:
            day = datetime.strptime(due_date, "%Y-%m-%d").replace(tzinfo=UTC)
            payload["due_date"] = int(day.timestamp() * 1000)
        created = await self._cu("POST", f"/list/{cu_list['id']}/task", json=payload)
        task = self._normalize_write_result(created, "task creation")
        arguments = {"project": project, "title": title, "assignee": assignee, "due_date": due_date}
        await report_tool_call("create_task", arguments, "external", result=task)
        return task

    async def update_task_status(self, task_id: str, status: str) -> dict:
        self._require_writes_enabled()
        team = await self._team()
        current = await self._cu("GET", f"/task/{task_id}")
        task_team_id = current.get("team_id")
        if task_team_id is None or not str(task_team_id).strip():
            raise ValueError(
                "Cannot verify the ClickUp task workspace because the task response has no "
                "team_id — no update was sent."
            )
        if str(task_team_id) != str(team["id"]):
            raise ValueError(
                f"ClickUp task {task_id!r} belongs to workspace {task_team_id}, not the "
                f"selected workspace {team['id']} — no update was sent."
            )
        cu_status = CANONICAL_TO_CLICKUP.get(status, status)
        updated = await self._cu("PUT", f"/task/{task_id}", json={"status": cu_status})
        task = self._normalize_write_result(updated, "status update")
        arguments = {"task_id": task_id, "status": status}
        await report_tool_call("update_task_status", arguments, "external", result=task)
        return task


BACKENDS: dict[str, type] = {"platform": PlatformTaskBackend, "clickup": ClickUpTaskBackend}


def get_task_backend() -> TaskBackend:
    name = os.environ.get("OPS_TASK_BACKEND", "platform")
    backend_cls = BACKENDS.get(name)
    if backend_cls is None:
        raise ValueError(f"Unknown OPS_TASK_BACKEND {name!r}. Valid values: {', '.join(BACKENDS)}")
    return backend_cls()
