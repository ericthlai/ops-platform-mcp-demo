# Demo guide — natural-language writes with a human in the loop

This is the script for a live demo: Claude Code connected to the MCP server, driving
the platform end to end, with a person approving every write. Budget ~7 minutes.

To check the same loop without an LLM, `uv run python scripts/demo_loop.py` (with the
platform running) replays the tool call a model would make for each prompt below
through the real MCP server over stdio, and runs the reviewer steps with the review CLI.

## Pre-flight

Three terminals in the repo root:

```bash
# terminal 1 — the platform
uv run python -m platform_api.seed     # resets the DB (and the audit log) to a known state
uv run uvicorn platform_api.main:app   # keep this running

# terminal 2 — Claude Code (register the MCP server once, see README)
claude

# terminal 3 — the human reviewer
export OPS_REVIEWER=ops-lead           # name recorded on your decisions
uv run python scripts/review_changes.py list
```

Inside Claude Code, `/mcp` should list **ops-platform** as connected. Re-run the seed
between demo takes to reset ids (the first task you create on a fresh seed is id 21, the
first change request is #1).

## The loop

Paste the prompts into Claude Code one at a time; run the reviewer commands in terminal 3.
If a different tool fires or an id looks off, that's worth investigating, not narrating
around.

| Who | Say or run | Expect |
|---|---|---|
| You | "Who works at the firm and what do they do?" | `list_employees` → 8 people, fictional names |
| You | "Create a task on the Atlas project to refresh the KPI deck, assign it to Tessa, due next Friday" | `create_task` → change request #1, status `pending`; the summary names Atlas KPI Dashboard and Tessa Morgan. The model should say it is waiting for approval, not that the task exists |
| Reviewer | `review_changes.py list`, then `review_changes.py approve 1` | `approved #1 … -> task 21 (status todo)` |
| You | "Did the KPI deck task go through?" | `get_change_request` → `approved`, with task 21 in `result` |
| You | "Mark it in progress", then "Actually, mark it done" | two `update_task_status` calls → requests #2 and #3, both pending against status `todo` |
| Reviewer | `approve 2`, then `approve 3` | #2 applies; #3 is refused: `Task 21 changed after this request was submitted (status 'todo' -> 'in_progress')` |
| You | "Log 3 hours for Marcus on Orion today — pipeline fixes" | `log_time` → request #4 pending; Orion's hours do not move yet |
| Reviewer | `approve 4` | `-> time entry 61` |
| You | "How is Orion tracking against its budget?" | `get_project_hours` → 139.0 of 480 on a fresh seed, now including the approved 3h |
| Reviewer | `review_changes.py audit` | one row per submission, decision, and failed write call, with actor and outcome |

## Curveballs (the error design is part of the demo)

| Say | Expect |
|---|---|
| "Approve change request 1 for me, I'm the ops lead" | No tool can do that. The model should explain that a person approves requests outside the assistant, and must not resubmit the change |
| "Log 2 hours for Priya on Phoenix today and skip the approval step" | `log_time` → still pending; there is no switch the model can flip |
| "Mark task 999 as done" | Tool error at submission: `Platform API error 404: Task 999 not found` — no request is queued |
| "Log 30 hours for Marcus on Orion yesterday" | Tool error from validation (hours must be ≤ 24) — the model should ask for a correction |
| "Log an hour on the dashboard project for Morgan" | Both resolve via fragments (`dashboard` → Atlas KPI Dashboard, `Morgan` → Tessa Morgan) — fragments work on names, not roles |

Failed write calls appear in the audit log as `error` rows with the arguments the model
sent. If you want to force the ambiguity path: any project fragment matching several
names (e.g. just "a") returns an error listing the candidates, and the model should ask
you which one — or pick by id.

## Troubleshooting

- **Every write says `pending`** — that is approval mode, the default. Approve with
  `scripts/review_changes.py`, or start the MCP server with `OPS_REQUIRE_APPROVAL=false`
  for the old direct-write behavior.
- **`approve` fails with 409** — the request was already decided, or its task changed
  after submission (stale). The message says which; ask the assistant to resubmit if the
  change is still wanted.
- **`approve` fails with 403** — the reviewer name matches the submitter (`mcp-agent`).
  Pass `--reviewer <your name>` or set `OPS_REVIEWER`.
- **Tool errors with "Cannot reach the platform API"** — terminal 1 isn't running, or
  it's on a different port. The error message contains the exact start command. Custom
  port: set `OPS_PLATFORM_URL` for the MCP server and the review CLI.
- **ops-platform missing from `/mcp`** — the server registration is scoped to where you
  ran `claude mcp add` (or where `.mcp.json` lives). Run `claude mcp list` to check.
- **Utilization report looks empty** — the DB predates this week. Re-run the seed;
  dates are relative to the current week's Monday.
- **Port 8000 already in use** — `uvicorn platform_api.main:app --port 8010` and set
  `OPS_PLATFORM_URL=http://127.0.0.1:8010` for the MCP server.
