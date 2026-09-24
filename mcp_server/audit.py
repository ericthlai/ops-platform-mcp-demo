"""Report the write-tool calls the platform cannot audit on its own.

The platform records every write it applies, every change request, and every review
decision in the same transaction as the change. Two cases never reach that path, so the
MCP server reports them to ``POST /audit-events``:

- a write tool that fails (unresolvable name, invalid status, 404/422 from the platform);
- a write applied in an external system (ClickUp), which the platform never sees.

Reporting is best effort: if the platform is unreachable the original result or error
still reaches the model, and a warning goes to stderr.
"""

import functools
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from . import platform_client
from .approvals import caller_headers

logger = logging.getLogger(__name__)


async def report_tool_call(
    tool: str,
    arguments: dict[str, Any],
    outcome: str,
    *,
    detail: str | None = None,
    result: dict | None = None,
) -> None:
    body = {
        "tool": tool,
        "arguments": arguments,
        "outcome": outcome,
        "detail": detail,
        "result": result,
    }
    try:
        await platform_client.request(
            "POST", "/audit-events", json=body, headers=caller_headers(tool)
        )
    except Exception as exc:  # never let audit reporting mask the tool's own outcome
        logger.warning("could not record %s audit event for %s: %s", outcome, tool, exc)


def audited_write(tool_fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Report a failed call of an MCP write tool, then re-raise the original error."""
    signature = inspect.signature(tool_fn)

    @functools.wraps(tool_fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await tool_fn(*args, **kwargs)
        except Exception as exc:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            await report_tool_call(
                tool_fn.__name__, dict(bound.arguments), "error", detail=str(exc)
            )
            raise

    return wrapper
