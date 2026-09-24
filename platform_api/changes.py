"""Change requests: writes that wait for a human reviewer before they touch any record.

``POST /change-requests`` validates a change exactly as the direct write would (so bad
ids and invalid values fail immediately with 404/422), snapshots the record an update
will modify, and stores the request as pending. Nothing else changes.

Decisions live under ``/admin/change-requests``. The MCP server never calls those
routes, so an assistant using the MCP tools can submit and read requests but cannot
approve or reject them. Approval re-checks the record: if it changed since submission
the request is marked stale and nothing is applied.
"""

from fastapi import APIRouter, HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlmodel import Session, SQLModel, select

from . import audit, operations
from .audit import CallerDep
from .db import SessionDep
from .models import (
    ApprovalDecision,
    AuditOutcome,
    ChangeAction,
    ChangeRequest,
    ChangeRequestCreate,
    ChangeStatus,
    RejectionDecision,
    Task,
    TaskCreate,
    TaskUpdate,
    TimeEntryCreate,
)

PAYLOAD_MODELS: dict[ChangeAction, type[SQLModel]] = {
    ChangeAction.create_task: TaskCreate,
    ChangeAction.update_task: TaskUpdate,
    ChangeAction.create_time_entry: TimeEntryCreate,
}

router = APIRouter(tags=["change requests"])
admin_router = APIRouter(prefix="/admin", tags=["review (human only)"])


def _validate_payload(change: ChangeRequestCreate) -> SQLModel:
    try:
        data = PAYLOAD_MODELS[change.action].model_validate(change.payload)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False)
        raise RequestValidationError(
            [{**error, "loc": ("body", "payload", *error["loc"])} for error in errors]
        ) from None
    needs_target = change.action == ChangeAction.update_task
    if needs_target and change.target_id is None:
        raise HTTPException(status_code=422, detail="update_task requires target_id")
    if not needs_target and change.target_id is not None:
        raise HTTPException(status_code=422, detail=f"{change.action} does not take target_id")
    if needs_target and not data.model_dump(exclude_unset=True):
        raise HTTPException(status_code=422, detail="update_task payload has no fields to change")
    return data


def _describe(
    session: Session, change: ChangeRequestCreate, data: SQLModel
) -> tuple[str, dict | None]:
    """Check every referenced record exists (404 otherwise) and return a human-readable
    summary plus the baseline snapshot for updates."""
    if isinstance(data, TaskCreate):
        project = operations.get_project_or_404(session, data.project_id)
        summary = f"Create task {data.title!r} on {project.name}"
        if data.assignee_id is not None:
            assignee = operations.get_employee_or_404(session, data.assignee_id)
            summary += f", assigned to {assignee.name}"
        else:
            summary += ", unassigned"
        if data.due_date is not None:
            summary += f", due {data.due_date.isoformat()}"
        return summary, None
    if isinstance(data, TaskUpdate):
        task = operations.get_task_or_404(session, change.target_id)
        updates = data.model_dump(mode="json", exclude_unset=True)
        if updates.get("assignee_id") is not None:
            operations.get_employee_or_404(session, updates["assignee_id"])
        current = audit.snapshot(task)
        if all(current[field] == value for field, value in updates.items()):
            already = ", ".join(f"{field} {value!r}" for field, value in updates.items())
            raise HTTPException(
                status_code=422,
                detail=f"No change: task {task.id} already has {already}. Nothing was queued.",
            )
        changes = ", ".join(
            f"{field} {current[field]!r} -> {value!r}"
            if field == "title"  # free text from the caller: quote and escape it
            else f"{field} {current[field]} -> {value}"
            for field, value in updates.items()
        )
        return f"Update task {task.id} {task.title!r}: {changes}", current
    assert isinstance(data, TimeEntryCreate)
    employee = operations.get_employee_or_404(session, data.employee_id)
    project = operations.get_project_or_404(session, data.project_id)
    summary = (
        f"Log {data.hours:g}h for {employee.name} on {project.name} on {data.date.isoformat()}"
    )
    if data.note:
        summary += f", note {data.note!r}"
    return summary, None


def _target(change: ChangeRequest, result: dict | None = None) -> str | None:
    if change.action == ChangeAction.update_task:
        return f"task {change.target_id}"
    if result is None:
        return None
    kind = "task" if change.action == ChangeAction.create_task else "time entry"
    return f"{kind} {result['id']}"


@router.post("/change-requests", response_model=ChangeRequest, status_code=201)
def submit_change_request(change: ChangeRequestCreate, session: SessionDep, caller: CallerDep):
    """Queue a write for human approval. Nothing changes until a reviewer approves it."""
    data = _validate_payload(change)
    summary, baseline = _describe(session, change, data)
    request = ChangeRequest(
        action=change.action,
        target_id=change.target_id,
        payload=data.model_dump(mode="json", exclude_unset=True),
        arguments=change.arguments,
        summary=summary,
        baseline=baseline,
        requested_by=caller.actor,
        requested_via=caller.tool,
        requested_at=audit.utc_now(),
    )
    session.add(request)
    session.flush()
    audit.record(
        session,
        actor=caller.actor,
        tool=caller.tool,
        action=change.action,
        outcome=AuditOutcome.pending,
        target=_target(request),
        change_request_id=request.id,
        arguments=change.arguments or request.payload,
        before=baseline,
        detail=summary,
    )
    session.commit()
    session.refresh(request)
    return request


@router.get("/change-requests", response_model=list[ChangeRequest])
def list_change_requests(session: SessionDep, status: ChangeStatus | None = None):
    query = select(ChangeRequest)
    if status is not None:
        query = query.where(ChangeRequest.status == status)
    return session.exec(query.order_by(ChangeRequest.id)).all()


@router.get("/change-requests/{request_id}", response_model=ChangeRequest)
def get_change_request(request_id: int, session: SessionDep):
    return _get_or_404(session, request_id)


# --- human review ------------------------------------------------------------------


def _get_or_404(session: Session, request_id: int) -> ChangeRequest:
    change = session.get(ChangeRequest, request_id)
    if change is None:
        raise HTTPException(status_code=404, detail=f"Change request {request_id} not found")
    return change


def _open_for_review(session: Session, request_id: int, reviewer: str) -> ChangeRequest:
    change = _get_or_404(session, request_id)
    if change.status != ChangeStatus.pending:
        raise HTTPException(
            status_code=409, detail=f"Change request {request_id} is already {change.status}"
        )
    if reviewer.casefold() == change.requested_by.casefold():
        raise HTTPException(
            status_code=403,
            detail=f"{reviewer!r} submitted change request {request_id} and cannot review it",
        )
    return change


def _stale_reason(session: Session, change: ChangeRequest) -> str | None:
    """Why an update can no longer be applied safely, or None if its record is unchanged."""
    if change.action != ChangeAction.update_task:
        return None
    task = session.get(Task, change.target_id)
    if task is None:
        return f"Task {change.target_id} no longer exists."
    current = audit.snapshot(task)
    diffs = [
        f"{field} {was!r} -> {current.get(field)!r}"
        for field, was in (change.baseline or {}).items()
        if current.get(field) != was
    ]
    if not diffs:
        return None
    return (
        f"Task {task.id} changed after this request was submitted ({'; '.join(diffs)}). "
        "Nothing was applied; submit a new request if the change is still wanted."
    )


def _apply(session: Session, change: ChangeRequest) -> tuple[dict | None, dict]:
    """Execute the queued write. Returns (before, after) snapshots."""
    data = PAYLOAD_MODELS[change.action].model_validate(change.payload)
    if change.action == ChangeAction.create_task:
        return None, audit.snapshot(operations.create_task(session, data))
    if change.action == ChangeAction.update_task:
        task = operations.get_task_or_404(session, change.target_id)
        before = audit.snapshot(task)
        return before, audit.snapshot(operations.update_task(session, task, data))
    return None, audit.snapshot(operations.create_time_entry(session, data))


def _decide(change: ChangeRequest, reviewer: str, status: ChangeStatus, note: str | None) -> None:
    change.status = status
    change.decided_by = reviewer
    change.decided_at = audit.utc_now()
    change.decision_note = note


@admin_router.post("/change-requests/{request_id}/approve", response_model=ChangeRequest)
def approve_change_request(request_id: int, decision: ApprovalDecision, session: SessionDep):
    """Apply a pending change. Refuses (409) if its record changed since submission."""
    change = _open_for_review(session, request_id, decision.reviewer)
    event = {
        "actor": decision.reviewer,
        "action": change.action,
        "change_request_id": change.id,
        "arguments": change.payload,
    }
    reason = _stale_reason(session, change)
    if reason is not None:
        _decide(change, decision.reviewer, ChangeStatus.stale, reason)
        current = session.get(Task, change.target_id)
        audit.record(
            session,
            **event,
            outcome=AuditOutcome.stale,
            target=_target(change),
            before=change.baseline,
            after=audit.snapshot(current),
            detail=reason,
        )
        session.commit()
        raise HTTPException(status_code=409, detail=reason)
    try:
        before, after = _apply(session, change)
    except HTTPException as exc:
        note = f"Could not apply: {exc.detail}. Nothing was changed."
        _decide(change, decision.reviewer, ChangeStatus.failed, note)
        audit.record(session, **event, outcome=AuditOutcome.failed, detail=note)
        session.commit()
        raise HTTPException(status_code=409, detail=note) from None
    _decide(change, decision.reviewer, ChangeStatus.approved, None)
    change.result = after
    audit.record(
        session,
        **event,
        outcome=AuditOutcome.approved,
        target=_target(change, after),
        before=before,
        after=after,
    )
    session.commit()
    session.refresh(change)
    return change


@admin_router.post("/change-requests/{request_id}/reject", response_model=ChangeRequest)
def reject_change_request(request_id: int, decision: RejectionDecision, session: SessionDep):
    """Decline a pending change; the reason is stored on the request and in the audit log."""
    change = _open_for_review(session, request_id, decision.reviewer)
    _decide(change, decision.reviewer, ChangeStatus.rejected, decision.reason)
    audit.record(
        session,
        actor=decision.reviewer,
        action=change.action,
        outcome=AuditOutcome.rejected,
        target=_target(change),
        change_request_id=change.id,
        arguments=change.payload,
        detail=decision.reason,
    )
    session.commit()
    session.refresh(change)
    return change
