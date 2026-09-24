"""HTTP plumbing for the mock platform API, plus name resolution against it."""

import os
from typing import Any

import httpx

from .resolution import pick_by_name

PLATFORM_URL = os.environ.get("OPS_PLATFORM_URL", "http://127.0.0.1:8000")

# Tests inject an httpx.ASGITransport here to run against the FastAPI app
# in-process; in normal operation it stays None and real HTTP is used.
_transport: httpx.AsyncBaseTransport | None = None


async def request(method: str, path: str, **kwargs: Any) -> Any:
    try:
        async with httpx.AsyncClient(
            base_url=PLATFORM_URL, transport=_transport, timeout=10.0
        ) as client:
            response = await client.request(method, path, **kwargs)
    except httpx.ConnectError:
        raise ValueError(
            f"Cannot reach the platform API at {PLATFORM_URL}. "
            "Start it with: uv run uvicorn platform_api.main:app"
        ) from None
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise ValueError(f"Platform API error {response.status_code}: {detail}")
    return response.json()


async def resolve_employee(name_or_id: str | int) -> dict:
    text = str(name_or_id).strip()
    if text.isdigit():
        return await request("GET", f"/employees/{text}")
    return pick_by_name("employee", await request("GET", "/employees"), text)


async def resolve_project(name_or_id: str | int) -> dict:
    text = str(name_or_id).strip()
    if text.isdigit():
        return await request("GET", f"/projects/{text}")
    return pick_by_name("project", await request("GET", "/projects"), text)
