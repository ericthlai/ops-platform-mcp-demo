"""SQLModel table definitions and request schemas for the four-table data model."""

from datetime import date
from enum import StrEnum

from pydantic import field_validator
from sqlmodel import Field, SQLModel


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
