"""Durable audit trail in the platform database.

The platform writes an audit event in the same transaction as every write it applies,
every change request it queues, and every review decision. Callers identify themselves
with the ``X-Ops-Actor`` header (and ``X-Ops-Tool`` when the call comes from an MCP tool);
both are self-reported because this demo API has no authentication.

There are no routes to edit or delete events. ``POST /audit-events`` exists only for the
two cases the platform never sees — a write-tool call that failed, and a write applied in
an external system — and it cannot record a successful platform write or a decision.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query
from sqlmodel import Session, SQLModel, select

from .db import SessionDep
from .models import AuditEvent, AuditOutcome, ToolCallReport

DEFAULT_ACTOR = "api"


@dataclass(frozen=True)
class Caller:
    actor: str
    tool: str | None = None


def get_caller(
    x_ops_actor: Annotated[str | None, Header()] = None,
    x_ops_tool: Annotated[str | None, Header()] = None,
) -> Caller:
    actor = (x_ops_actor or "").strip() or DEFAULT_ACTOR
    tool = (x_ops_tool or "").strip() or None
    return Caller(actor=actor, tool=tool)


CallerDep = Annotated[Caller, Depends(get_caller)]


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def snapshot(record: SQLModel | None) -> dict[str, Any] | None:
    """JSON-safe copy of a row, used for before/after images."""
    return None if record is None else record.model_dump(mode="json")


def record(
    session: Session,
    *,
    actor: str,
    action: str,
    outcome: AuditOutcome,
    tool: str | None = None,
    target: str | None = None,
    change_request_id: int | None = None,
    arguments: dict[str, Any] | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    detail: str | None = None,
) -> AuditEvent:
    """Stage an audit event on the caller's session; it commits with the change."""
    event = AuditEvent(
        occurred_at=utc_now(),
        actor=actor,
        tool=tool,
        action=action,
        outcome=outcome,
        target=target,
        change_request_id=change_request_id,
        arguments=arguments,
        before=before,
        after=after,
        detail=detail,
    )
    session.add(event)
    return event


router = APIRouter(tags=["audit"])


@router.get("/audit-events", response_model=list[AuditEvent])
def list_audit_events(
    session: SessionDep,
    change_request_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
):
    """Most recent events first."""
    query = select(AuditEvent)
    if change_request_id is not None:
        query = query.where(AuditEvent.change_request_id == change_request_id)
    return session.exec(query.order_by(AuditEvent.id.desc()).limit(limit)).all()


@router.post("/audit-events", response_model=AuditEvent, status_code=201)
def report_tool_call(report: ToolCallReport, session: SessionDep, caller: CallerDep):
    """Record a failed write-tool call, or a write applied outside the platform."""
    event = record(
        session,
        actor=caller.actor,
        tool=report.tool,
        action=report.tool,
        outcome=report.outcome,
        arguments=report.arguments,
        after=report.result,
        detail=report.detail,
    )
    session.commit()
    session.refresh(event)
    return event
