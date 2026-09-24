"""Replay the DEMO.md loop through the real MCP server over stdio, without an LLM.

Each step is the tool call a model would make for the DEMO.md prompt shown next to it.
The MCP server is launched exactly as an MCP client would launch it, so this exercises
the full path: MCP protocol -> tool handler -> name resolution -> platform HTTP API.

Pre-flight (same as DEMO.md):

    uv run python -m platform_api.seed
    uv run uvicorn platform_api.main:app      # keep running in another terminal
    uv run python scripts/demo_loop.py
"""

import asyncio
import json
import sys
from datetime import date, timedelta

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def next_friday(today: date) -> date:
    return today + timedelta(days=(4 - today.weekday()) % 7 or 7)


NAMES: dict[int, str] = {}


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
        NAMES.update({e["id"]: e["name"] for e in data})
        return f"{len(data)} employees, e.g. " + ", ".join(e["name"] for e in data[:3])
    if tool in ("create_task", "update_task_status"):
        who = NAMES.get(data.get("assignee_id"), data.get("assignee_id"))
        return f"task {data['id']} '{data['title']}' status={data['status']} assignee={who}"
    if tool == "log_time":
        return f"time entry {data['id']}: {data['hours']}h on {data['date']}"
    if tool == "get_project_hours":
        return (
            f"{data['project_name']}: {data['logged_hours']} of {data['budget_hours']} budget hours"
        )
    if tool == "utilization_report":
        rows = data["employees"]
        low = min(rows, key=lambda r: r["utilization_pct"])
        marcus = next(r for r in rows if r["name"].startswith("Marcus"))
        return (
            f"{data['week']}: {len(rows)} rows; lowest: {low['name']} {low['utilization_pct']}%;"
            f" Marcus {marcus['logged_hours']}h"
        )
    return text[:200]


async def main() -> None:
    today = date.today()
    steps = [
        ("Who works at the firm and what do they do?", "list_employees", {}),
        (
            "Create a task on the Atlas project to refresh the KPI deck, assign it to Tessa, "
            "due next Friday",
            "create_task",
            {
                "project": "Atlas",
                "title": "Refresh the KPI deck",
                "assignee": "Tessa",
                "due_date": next_friday(today).isoformat(),
            },
        ),
        (
            "Actually, mark that in progress",
            "update_task_status",
            {"task_id": "21", "status": "in_progress"},
        ),
        (
            "Log 3 hours for Marcus on Orion today - pipeline fixes",
            "log_time",
            {
                "employee": "Marcus",
                "project": "Orion",
                "date": today.isoformat(),
                "hours": 3,
                "note": "Pipeline fixes",
            },
        ),
        ("How is Orion tracking against its budget?", "get_project_hours", {"project": "Orion"}),
        ("Pull this week's utilization report", "utilization_report", {}),
        ("Mark task 999 as done", "update_task_status", {"task_id": "999", "status": "done"}),
        (
            "Log 30 hours for Marcus on Orion yesterday",
            "log_time",
            {
                "employee": "Marcus",
                "project": "Orion",
                "date": (today - timedelta(days=1)).isoformat(),
                "hours": 30,
            },
        ),
        (
            "Log an hour on project 'a' for Marcus",
            "log_time",
            {"employee": "Marcus", "project": "a", "date": today.isoformat(), "hours": 1},
        ),
    ]
    sys.stdout.reconfigure(encoding="utf-8")
    server = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.server"])
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        print(f"connected: {len(tools.tools)} MCP tools\n")
        for prompt, tool, args in steps:
            result = await session.call_tool(tool, args)
            print(f'> "{prompt}"')
            print(f"  {tool}({', '.join(f'{k}={v!r}' for k, v in args.items())})")
            print(f"  -> {summarize(tool, result)}\n")


if __name__ == "__main__":
    asyncio.run(main())
