"""Write operations shared by the direct endpoints and change-request approval.

Each operation validates the records it references (404 if missing), stages the change on
the session, and flushes so new rows get ids. None of them commit: the caller commits the
change together with its audit event, so a write can never land without its audit row.
"""

from fastapi import HTTPException
from sqlmodel import Session

from .models import Employee, Project, Task, TaskCreate, TaskUpdate, TimeEntry, TimeEntryCreate


def get_employee_or_404(session: Session, employee_id: int) -> Employee:
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail=f"Employee {employee_id} not found")
    return employee


def get_project_or_404(session: Session, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return project


def get_task_or_404(session: Session, task_id: int) -> Task:
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return task


def create_task(session: Session, data: TaskCreate) -> Task:
    get_project_or_404(session, data.project_id)
    if data.assignee_id is not None:
        get_employee_or_404(session, data.assignee_id)
    task = Task.model_validate(data)
    session.add(task)
    session.flush()
    return task


def update_task(session: Session, task: Task, data: TaskUpdate) -> Task:
    updates = data.model_dump(exclude_unset=True)
    if updates.get("assignee_id") is not None:
        get_employee_or_404(session, updates["assignee_id"])
    for field, value in updates.items():
        setattr(task, field, value)
    session.add(task)
    session.flush()
    return task


def create_time_entry(session: Session, data: TimeEntryCreate) -> TimeEntry:
    get_employee_or_404(session, data.employee_id)
    get_project_or_404(session, data.project_id)
    entry = TimeEntry.model_validate(data)
    session.add(entry)
    session.flush()
    return entry
