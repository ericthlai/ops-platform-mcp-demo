"""FastAPI app exposing the platform's REST API: the four business tables, reports, and
the governance routes (change requests, human review, audit log)."""

import re
from contextlib import asynccontextmanager
from datetime import date, timedelta

from fastapi import FastAPI, HTTPException
from sqlalchemy import func
from sqlmodel import Session, SQLModel, select

from . import audit, changes, operations
from .audit import Caller, CallerDep
from .db import SessionDep, create_db_and_tables
from .models import (
    AuditOutcome,
    Employee,
    EmployeeCreate,
    Project,
    ProjectCreate,
    Task,
    TaskCreate,
    TaskStatus,
    TaskUpdate,
    TimeEntry,
    TimeEntryCreate,
)

WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    yield


app = FastAPI(title="Ops Platform API", lifespan=lifespan)
app.include_router(changes.router)
app.include_router(changes.admin_router)
app.include_router(audit.router)


def _audit_applied(
    session: Session,
    caller: Caller,
    action: str,
    target: str,
    data: SQLModel,
    record: SQLModel,
    before: dict | None = None,
) -> None:
    """Audit a direct (non-queued) write in the same transaction as the write itself."""
    audit.record(
        session,
        actor=caller.actor,
        tool=caller.tool,
        action=action,
        outcome=AuditOutcome.applied,
        target=target,
        arguments=data.model_dump(mode="json", exclude_unset=True),
        before=before,
        after=audit.snapshot(record),
    )


# --- employees ---------------------------------------------------------------


@app.get("/employees", response_model=list[Employee])
def list_employees(session: SessionDep):
    return session.exec(select(Employee)).all()


@app.post("/employees", response_model=Employee, status_code=201)
def create_employee(data: EmployeeCreate, session: SessionDep, caller: CallerDep):
    employee = Employee.model_validate(data)
    session.add(employee)
    session.flush()
    _audit_applied(session, caller, "create_employee", f"employee {employee.id}", data, employee)
    session.commit()
    session.refresh(employee)
    return employee


@app.get("/employees/{employee_id}", response_model=Employee)
def get_employee(employee_id: int, session: SessionDep):
    return operations.get_employee_or_404(session, employee_id)


# --- projects ----------------------------------------------------------------


@app.get("/projects", response_model=list[Project])
def list_projects(session: SessionDep):
    return session.exec(select(Project)).all()


@app.post("/projects", response_model=Project, status_code=201)
def create_project(data: ProjectCreate, session: SessionDep, caller: CallerDep):
    project = Project.model_validate(data)
    session.add(project)
    session.flush()
    _audit_applied(session, caller, "create_project", f"project {project.id}", data, project)
    session.commit()
    session.refresh(project)
    return project


@app.get("/projects/{project_id}", response_model=Project)
def get_project(project_id: int, session: SessionDep):
    return operations.get_project_or_404(session, project_id)


@app.get("/projects/{project_id}/hours")
def project_hours(project_id: int, session: SessionDep):
    """Logged hours vs budget for one project."""
    project = operations.get_project_or_404(session, project_id)
    logged = session.exec(
        select(func.coalesce(func.sum(TimeEntry.hours), 0)).where(
            TimeEntry.project_id == project_id
        )
    ).one()
    return {
        "project_id": project.id,
        "project_name": project.name,
        "client": project.client,
        "status": project.status,
        "budget_hours": project.budget_hours,
        "logged_hours": logged,
        "remaining_hours": project.budget_hours - logged,
    }


# --- tasks -------------------------------------------------------------------


@app.get("/tasks", response_model=list[Task])
def list_tasks(
    session: SessionDep,
    project_id: int | None = None,
    assignee_id: int | None = None,
    status: TaskStatus | None = None,
):
    query = select(Task)
    if project_id is not None:
        query = query.where(Task.project_id == project_id)
    if assignee_id is not None:
        query = query.where(Task.assignee_id == assignee_id)
    if status is not None:
        query = query.where(Task.status == status)
    return session.exec(query).all()


@app.post("/tasks", response_model=Task, status_code=201)
def create_task(data: TaskCreate, session: SessionDep, caller: CallerDep):
    task = operations.create_task(session, data)
    _audit_applied(session, caller, "create_task", f"task {task.id}", data, task)
    session.commit()
    session.refresh(task)
    return task


@app.patch("/tasks/{task_id}", response_model=Task)
def update_task(task_id: int, data: TaskUpdate, session: SessionDep, caller: CallerDep):
    task = operations.get_task_or_404(session, task_id)
    before = audit.snapshot(task)
    operations.update_task(session, task, data)
    _audit_applied(session, caller, "update_task", f"task {task.id}", data, task, before)
    session.commit()
    session.refresh(task)
    return task


# --- time entries ------------------------------------------------------------


@app.post("/time-entries", response_model=TimeEntry, status_code=201)
def create_time_entry(data: TimeEntryCreate, session: SessionDep, caller: CallerDep):
    entry = operations.create_time_entry(session, data)
    _audit_applied(session, caller, "create_time_entry", f"time entry {entry.id}", data, entry)
    session.commit()
    session.refresh(entry)
    return entry


# --- reports -----------------------------------------------------------------


def _parse_week(week: str | None) -> tuple[str, date]:
    """Resolve an ISO-week string like '2026-W24' (default: current week) to its Monday."""
    if week is None:
        iso = date.today().isocalendar()
        return f"{iso.year}-W{iso.week:02d}", date.fromisocalendar(iso.year, iso.week, 1)
    match = WEEK_RE.match(week)
    if match is None:
        raise HTTPException(
            status_code=422, detail=f"week must be an ISO week like 2026-W24, got {week!r}"
        )
    try:
        monday = date.fromisocalendar(int(match[1]), int(match[2]), 1)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{week!r} is not a valid ISO week") from None
    return week, monday


@app.get("/reports/utilization")
def utilization_report(session: SessionDep, week: str | None = None):
    """Per-employee logged hours vs weekly capacity for one ISO week."""
    label, monday = _parse_week(week)
    sunday = monday + timedelta(days=6)
    logged_by_employee = dict(
        session.exec(
            select(TimeEntry.employee_id, func.sum(TimeEntry.hours))
            .where(TimeEntry.date >= monday, TimeEntry.date <= sunday)
            .group_by(TimeEntry.employee_id)
        ).all()
    )
    rows = []
    for employee in session.exec(select(Employee)).all():
        logged = logged_by_employee.get(employee.id, 0)
        capacity = employee.weekly_capacity_hours
        rows.append(
            {
                "employee_id": employee.id,
                "name": employee.name,
                "capacity_hours": capacity,
                "logged_hours": logged,
                "utilization_pct": round(logged / capacity * 100, 1) if capacity else None,
            }
        )
    return {
        "week": label,
        "week_start": monday.isoformat(),
        "week_end": sunday.isoformat(),
        "employees": rows,
    }
