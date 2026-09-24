"""SQLModel table definitions and request schemas.

Four business tables (employees, projects, tasks, time entries) plus two governance
tables: change requests (writes waiting for human approval) and audit events.
"""

from datetime import date
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import StringConstraints, field_validator
from sqlmodel import JSON, Column, Field, SQLModel

NonBlankStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ProjectStatus(StrEnum):
    active = "active"
    on_hold = "on_hold"
    closed = "closed"


class TaskStatus(StrEnum):
    todo = "todo"
    in_progress = "in_progress"
    done = "done"


class EmployeeBase(SQLModel):
    name: str
    role: str
    department: str
    weekly_capacity_hours: float = Field(ge=0, le=80)


class Employee(EmployeeBase, table=True):
    __tablename__ = "employees"

    id: int | None = Field(default=None, primary_key=True)


class EmployeeCreate(EmployeeBase):
    pass


class ProjectBase(SQLModel):
    name: str
    client: str
    status: ProjectStatus = ProjectStatus.active
    budget_hours: float = Field(ge=0)


class Project(ProjectBase, table=True):
    __tablename__ = "projects"

    id: int | None = Field(default=None, primary_key=True)


class ProjectCreate(ProjectBase):
    pass


class TimeEntryBase(SQLModel):
    employee_id: int = Field(foreign_key="employees.id")
    project_id: int = Field(foreign_key="projects.id")
    date: date
    hours: float = Field(gt=0, le=24)
    note: str | None = None


class TimeEntry(TimeEntryBase, table=True):
    __tablename__ = "time_entries"

    id: int | None = Field(default=None, primary_key=True)


class TimeEntryCreate(TimeEntryBase):
    pass


class TaskBase(SQLModel):
    project_id: int = Field(foreign_key="projects.id")
    assignee_id: int | None = Field(default=None, foreign_key="employees.id")
    title: str
    status: TaskStatus = TaskStatus.todo
    due_date: date | None = None


class Task(TaskBase, table=True):
    __tablename__ = "tasks"

    id: int | None = Field(default=None, primary_key=True)


class TaskCreate(TaskBase):
    pass


class TaskUpdate(SQLModel):
    """Partial update for PATCH /tasks/{id}; only provided fields are applied."""

    title: str | None = None
    status: TaskStatus | None = None
    assignee_id: int | None = None
    due_date: date | None = None

    @field_validator("title", "status")
    @classmethod
    def reject_null_required_fields(cls, value: str | None) -> str:
        # Defaults allow omission in PATCH; explicit null cannot clear required columns.
        if value is None:
            raise ValueError("title and status must not be null when provided")
        return value


# --- governance: change requests and audit events ------------------------------


class ChangeAction(StrEnum):
    """Platform writes that can be queued for approval."""

    create_task = "create_task"
    update_task = "update_task"
    create_time_entry = "create_time_entry"


class ChangeStatus(StrEnum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    stale = "stale"
    failed = "failed"


class ChangeRequest(SQLModel, table=True):
    """A write that waits for a human reviewer. ``payload`` is the validated platform
    body (resolved ids); ``arguments`` is what the caller originally asked for;
    ``baseline`` snapshots the record an update will modify, for the staleness check."""

    __tablename__ = "change_requests"

    id: int | None = Field(default=None, primary_key=True)
    action: ChangeAction
    target_id: int | None = None
    payload: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    arguments: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    summary: str
    baseline: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    status: ChangeStatus = Field(default=ChangeStatus.pending, index=True)
    requested_by: str
    requested_via: str | None = None
    requested_at: str
    decided_by: str | None = None
    decided_at: str | None = None
    decision_note: str | None = None
    result: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))


class ChangeRequestCreate(SQLModel):
    action: ChangeAction
    target_id: int | None = None
    payload: dict[str, Any]
    arguments: dict[str, Any] | None = None


class ApprovalDecision(SQLModel):
    reviewer: NonBlankStr


class RejectionDecision(ApprovalDecision):
    reason: NonBlankStr


class AuditOutcome(StrEnum):
    applied = "applied"  # direct write executed
    pending = "pending"  # change request submitted; nothing changed yet
    approved = "approved"  # change request approved and executed
    rejected = "rejected"  # reviewer declined; reason in detail
    stale = "stale"  # refused at approval: the record changed after submission
    failed = "failed"  # approval could not be executed; nothing changed
    error = "error"  # write-tool call failed before anything changed (reported by MCP)
    external = "external"  # write applied in an external system, e.g. ClickUp (reported by MCP)


class AuditEvent(SQLModel, table=True):
    """Append-only record of a write or a review decision. Timestamps are UTC ISO-8601."""

    __tablename__ = "audit_events"

    id: int | None = Field(default=None, primary_key=True)
    occurred_at: str
    actor: str
    tool: str | None = None
    action: str
    outcome: AuditOutcome
    target: str | None = None
    change_request_id: int | None = Field(default=None, index=True)
    arguments: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    before: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    after: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    detail: str | None = None


class ToolCallReport(SQLModel):
    """A write-tool call the platform could not record itself (see POST /audit-events)."""

    tool: NonBlankStr
    arguments: dict[str, Any] | None = None
    outcome: Literal[AuditOutcome.error, AuditOutcome.external]
    detail: str | None = None
    result: dict[str, Any] | None = None
