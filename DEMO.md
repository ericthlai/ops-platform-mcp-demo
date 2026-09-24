# Demo guide — the full CRUD loop in natural language

This is the script for a live demo: Claude Code connected to the MCP server, driving
the platform end to end. Budget ~5 minutes.

To check the same loop without an LLM, `uv run python scripts/demo_loop.py` (with the
platform running) replays the tool call a model would make for each prompt below
through the real MCP server over stdio.

## Pre-flight

Two terminals in the repo root:

```bash
# terminal 1 — the platform
uv run python -m platform_api.seed     # resets the DB to a known state
uv run uvicorn platform_api.main:app   # keep this running

# terminal 2 — Claude Code (register the MCP server once, see README)
claude
```

Inside Claude Code, `/mcp` should list **ops-platform** as connected. Re-run the seed
between demo takes to reset ids (the first task you create on a fresh seed is id 21).

## The loop

Paste these one at a time. The right column is what should happen — if a different
tool fires or an id looks off, that's worth investigating, not narrating around.

| Say | Expect |
|---|---|
| "Who works at the firm and what do they do?" | `list_employees` → 8 people, fictional names |
| "Create a task on the Atlas project to refresh the KPI deck, assign it to Tessa, due next Friday" | `create_task` → returns the new task (id 21 on a fresh seed), status `todo`, assignee resolved to Tessa Morgan |
| "Actually, mark that in progress" | `update_task_status` → status `in_progress`; the model should reuse the id it just got back |
| "Log 3 hours for Marcus on Orion today — pipeline fixes" | `log_time` → entry created; employee/project resolved from fragments |
| "How is Orion tracking against its budget?" | `get_project_hours` → logged includes the 3h you just added (139.0 on a fresh seed) |
| "Pull this week's utilization report — who's under-loaded?" | `utilization_report` → 8 rows; Marcus's logged hours reflect the +3 |

## Curveballs (the error design is part of the demo)

| Say | Expect |
|---|---|
| "Mark task 999 as done" | Tool error: `Platform API error 404: Task 999 not found` — the model should say so, not retry blindly |
| "Log 30 hours for Marcus on Orion yesterday" | Tool error from validation (hours must be ≤ 24) — the model should ask for a correction |
| "Log an hour on the dashboard project for Morgan" | Both resolve via fragments (`dashboard` → Atlas KPI Dashboard, `Morgan` → Tessa Morgan) — fragments work on names, not roles |

If you want to force the ambiguity path: any project fragment matching several names
(e.g. just "a") returns an error listing the candidates, and the model should ask you
which one — or pick by id.

## Troubleshooting

- **Tool errors with "Cannot reach the platform API"** — terminal 1 isn't running, or
  it's on a different port. The error message contains the exact start command. Custom
  port: set `OPS_PLATFORM_URL` for the MCP server.
- **ops-platform missing from `/mcp`** — the server registration is scoped to where you
  ran `claude mcp add` (or where `.mcp.json` lives). Run `claude mcp list` to check.
- **Utilization report looks empty** — the DB predates this week. Re-run the seed;
  dates are relative to the current week's Monday.
- **Port 8000 already in use** — `uvicorn platform_api.main:app --port 8010` and set
  `OPS_PLATFORM_URL=http://127.0.0.1:8010` for the MCP server.
