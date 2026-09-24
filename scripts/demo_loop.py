"""Replay the DEMO.md loop through the real MCP server over stdio, without an LLM.

Each assistant step is the tool call a model would make for the DEMO.md prompt shown next
to it. The MCP server is launched exactly as an MCP client would launch it, so this
exercises the full path: MCP protocol -> tool handler -> name resolution -> platform API.
Writes come back as pending change requests; the reviewer steps run the human review CLI
(scripts/review_changes.py), which is the only way to approve them.

Pre-flight (same as DEMO.md):

    uv run python -m platform_api.seed
    uv run uvicorn platform_api.main:app      # keep running in another terminal
    uv run python scripts/demo_loop.py
"""

import asyncio
import json
import os
import shlex
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEWER = "ops-lead"


def next_friday(today: date) -> date:
    return today + timedelta(days=(4 - today.weekday()) % 7 or 7)


def summarize(tool: str, result) -> str:
    text = "\n".join(c.text for c in result.content if getattr(c, "text", None))
    if result.isError:
        return f"ERROR  {text}"
    data = (
        json.loads(text)
        if len(result.content) == 1
        else [json.loads(c.text) for c in result.content]
    )
    if tool == "list_employees":
        return f"{len(data)} employees, e.g. " + ", ".join(e["name"] for e in data[:3])
    if isinstance(data, dict) and "change_request_id" in data:
        line = f"change request #{data['change_request_id']} {data['status']}"
        if data["status"] == "pending":
            return f"{line}: {data['summary']}"
        line += f" by {data['decided_by']}"
        if data["result"]:
            task = data["result"]
            return f"{line}: task {task['id']} '{task['title']}' status={task['status']}"
        return f"{line}: {data['decision_note']}"
    if tool == "get_project_hours":
        return (
            f"{data['project_name']}: {data['logged_hours']} of {data['budget_hours']} budget hours"
        )
    return text[:200]


def review(*args: str) -> None:
    """Run the human review CLI exactly as a reviewer would type it."""
    print(f"  $ python scripts/review_changes.py {shlex.join(args)}")
    completed = subprocess.run(
        [sys.executable, "scripts/review_changes.py", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "OPS_REVIEWER": REVIEWER},
    )
    for line in (completed.stdout + completed.stderr).strip().splitlines():
        print(f"    {line}")
    print()


async def main() -> None:
    today = date.today()
    sys.stdout.reconfigure(encoding="utf-8")
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.server"],
        env={**os.environ, "OPS_REQUIRE_APPROVAL": "true"},
    )
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        print(f"connected: {len(tools.tools)} MCP tools, approval mode on\n")

        async def say(prompt: str, tool: str, args: dict) -> None:
            result = await session.call_tool(tool, args)
            print(f'> "{prompt}"')
            print(f"  {tool}({', '.join(f'{k}={v!r}' for k, v in args.items())})")
            print(f"  -> {summarize(tool, result)}\n")

        def reviewer(note: str) -> None:
            print(f"[reviewer, outside the assistant: {note}]")

        await say("Who works at the firm and what do they do?", "list_employees", {})
        await say(
            "Create a task on the Atlas project to refresh the KPI deck, assign it to Tessa, "
            "due next Friday",
            "create_task",
            {
                "project": "Atlas",
                "title": "Refresh the KPI deck",
                "assignee": "Tessa",
                "due_date": next_friday(today).isoformat(),
            },
        )
        reviewer("see what is waiting, then approve it")
        review("list")
        review("approve", "1")
        await say(
            "Did the KPI deck task go through?", "get_change_request", {"change_request_id": 1}
        )
        await say(
            "Mark it in progress", "update_task_status", {"task_id": "21", "status": "in_progress"}
        )
        await say(
            "Actually, mark it done", "update_task_status", {"task_id": "21", "status": "done"}
        )
        reviewer("approve both; the second was submitted against the old status")
        review("approve", "2")
        review("approve", "3")
        await say(
            "Log 3 hours for Marcus on Orion today - pipeline fixes",
            "log_time",
            {
                "employee": "Marcus",
                "project": "Orion",
                "date": today.isoformat(),
                "hours": 3,
                "note": "Pipeline fixes",
            },
        )
        await say(
            "Log 2 hours for Felix on Quartz today",
            "log_time",
            {"employee": "Felix", "project": "Quartz", "date": today.isoformat(), "hours": 2},
        )
        reviewer("approve the Orion hours, reject the Quartz hours")
        review("approve", "4")
        review("reject", "5", "--reason", "Quartz is on hold, no new hours this month")
        await say(
            "How is Orion tracking against its budget?", "get_project_hours", {"project": "Orion"}
        )
        await say(
            "What happened to the Quartz hours?", "get_change_request", {"change_request_id": 5}
        )
        await say(
            "Mark task 999 as done", "update_task_status", {"task_id": "999", "status": "done"}
        )
        await say(
            "Log 30 hours for Marcus on Orion yesterday",
            "log_time",
            {
                "employee": "Marcus",
                "project": "Orion",
                "date": (today - timedelta(days=1)).isoformat(),
                "hours": 30,
            },
        )
        await say(
            "Log an hour on project 'a' for Marcus",
            "log_time",
            {"employee": "Marcus", "project": "a", "date": today.isoformat(), "hours": 1},
        )
        reviewer("read the audit trail")
        review("audit", "--limit", "30")


if __name__ == "__main__":
    asyncio.run(main())
