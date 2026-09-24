# ops-platform-mcp-demo

[![CI](https://github.com/ericthlai/ops-platform-mcp-demo/actions/workflows/ci.yml/badge.svg)](https://github.com/ericthlai/ops-platform-mcp-demo/actions/workflows/ci.yml)

An **MCP server** that lets an AI assistant read and write business records in plain
language — look up people, create and move tasks, log time, pull utilization — through
a synthetic operations platform (FastAPI + SQLite) and an optional, safe-by-default
adapter for the real ClickUp API.

## Demo

Output of [`scripts/demo_loop.py`](scripts/demo_loop.py) on a fresh seed (2026-09-23).
Each prompt, taken from the [DEMO.md](DEMO.md) script, is paired with the tool call a
model makes for it and sent through the real MCP server over stdio; the arrow lines are
the server's actual responses (excerpt, long lines wrapped).

```text
connected: 8 MCP tools

> "Create a task on the Atlas project to refresh the KPI deck, assign it to Tessa, due next Friday"
  create_task(project='Atlas', title='Refresh the KPI deck', assignee='Tessa', due_date='2026-09-25')
  -> task 21 'Refresh the KPI deck' status=todo assignee=Tessa Morgan

> "Actually, mark that in progress"
  update_task_status(task_id='21', status='in_progress')
  -> task 21 'Refresh the KPI deck' status=in_progress assignee=Tessa Morgan

> "Log 3 hours for Marcus on Orion today - pipeline fixes"
  log_time(employee='Marcus', project='Orion', date='2026-09-23', hours=3, note='Pipeline fixes')
  -> time entry 61: 3.0h on 2026-09-23

> "How is Orion tracking against its budget?"
  get_project_hours(project='Orion')
  -> Orion Data Migration: 139.0 of 480.0 budget hours

> "Mark task 999 as done"
  update_task_status(task_id='999', status='done')
  -> ERROR  Error executing tool update_task_status: Platform API error 404: Task 999 not found

> "Log an hour on project 'a' for Marcus"
  log_time(employee='Marcus', project='a', date='2026-09-23', hours=1)
  -> ERROR  Error executing tool log_time: Ambiguous project 'a' — matches: Orion Data Migration (id 1);
     Atlas KPI Dashboard (id 2); Quartz CRM Cleanup (id 3); Phoenix Onboarding Portal (id 4);
     Legacy Archive Extract (id 5). Retry with the exact name or the id.
```

Name fragments ("Tessa", "Orion") resolve to records; unknown ids and ambiguous names
come back as errors the model can act on instead of silent guesses. The full run,
including the 30-hour validation error and the utilization report, is reproducible with
the commands in [Quickstart](#quickstart).

## What it is

```
FastAPI "platform" (SQLite + seed)  ◄─HTTP─►  MCP server (stdio)  ◄─►  Claude Code / any MCP client
                                                    │
                                                    └─ optional: ClickUp API adapter for task tools
```

- **Platform API** — a small operations system of record: employees, projects, tasks,
  time entries, plus budget and utilization reports. All data is synthetic.
- **MCP server** — eight tools whose descriptions are written for a model. The server
  talks to the platform only over HTTP, the way a connector wraps a vendor API.
- **Adapter pattern** — the three task tools can be served by the real ClickUp API
  without changing the tool surface. ClickUp writes are off unless explicitly enabled.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) (it will fetch Python 3.12 automatically).

```bash
uv sync                                # install dependencies
uv run python -m platform_api.seed    # create + seed ops_platform.db (re-run anytime to reset)
uv run uvicorn platform_api.main:app  # serve the platform API on http://127.0.0.1:8000
uv run python scripts/demo_loop.py    # in a second terminal: replay the demo loop over MCP
```

Interactive API docs: <http://127.0.0.1:8000/docs>

The seed is deterministic (no randomness) with **relative dates**: time entries are
fixed offsets from the current week's Monday and task due dates are offsets from today,
so "this week's utilization" always has data no matter when you run the demo. All
people, clients, projects, and time entries are fictional.

> **Local demo boundary:** the mock API intentionally has no authentication. Keep it
> on `127.0.0.1`; do not expose it to a network or load production data into it. See
> [SECURITY.md](SECURITY.md) for the full threat-model boundary.

## Connect the MCP server to Claude Code

The platform API must be running first (see Quickstart). Then either register the
server with the CLI:

```bash
claude mcp add ops-platform -- uv run --directory /absolute/path/to/ops-platform-mcp-demo python -m mcp_server.server
```

…or drop a `.mcp.json` next to wherever you run `claude` (if that's this repo root,
the relative directory works):

```json
{
  "mcpServers": {
    "ops-platform": {
      "command": "uv",
      "args": ["run", "--directory", ".", "python", "-m", "mcp_server.server"]
    }
  }
}
```

Then ask Claude Code things like:

> list the employees · create a task on Atlas to "refresh the KPI deck" for Tessa ·
> mark task 21 in progress · log 3 hours for Marcus on Orion today · pull this week's
> utilization report

A scripted version of this loop — including the error-path curveballs and a
troubleshooting section — is in [DEMO.md](DEMO.md).

### Tools

| Tool | Kind | Notes |
|---|---|---|
| `list_employees` | read | full roster with capacity |
| `list_projects` | read | id, client, status, budget |
| `list_tasks(project?, assignee?, status?)` | read | names or ids accepted; results include names |
| `get_project_hours(project)` | read | logged vs budget |
| `utilization_report(week?)` | read | ISO week e.g. `2026-W24`, defaults to current week |
| `create_task(project, title, assignee?, due_date?)` | write | returns created task |
| `update_task_status(task_id, status)` | write | todo / in_progress / done |
| `log_time(employee, project, date, hours, note?)` | write | returns created entry |

Tools accept human-friendly names where reasonable and resolve them to ids internally;
ambiguous or unknown names return errors that list the candidates so the model can
self-correct.

### Task backends (the adapter pattern)

The three task tools (`list_tasks`, `create_task`, `update_task_status`) are
backend-pluggable — same tool surface, different system of record:

```bash
OPS_TASK_BACKEND=platform   # default: the mock platform above
OPS_TASK_BACKEND=clickup    # real ClickUp API (set CLICKUP_API_TOKEN)
```

In ClickUp mode a "project" is a ClickUp **list** (found by name across all spaces
and folders), an assignee is a workspace member, statuses map todo / in_progress /
done ↔ "to do" / "in progress" / "complete", and task ids are ClickUp's alphanumeric
strings. Get a personal token from ClickUp → Settings → Apps and see `.env.example`.
The other five tools (employees, hours, time, utilization) always use the platform.

Copy the environment template, then ask `uv` to load it explicitly:

```bash
cp .env.example .env
# Edit .env, then run the MCP server directly for a smoke test:
uv run --env-file .env python -m mcp_server.server
```

`uv` does not load `.env` implicitly. When ClickUp mode is launched through an MCP
client, add `--env-file /absolute/path/to/.env` to the `uv run` arguments or inject the
same variables through the client's environment configuration.

ClickUp reads are available once a token is configured, but `create_task` and
`update_task_status` fail closed until `CLICKUP_WRITES_ENABLED=true` is explicitly set.
If a token can access multiple workspaces, `CLICKUP_TEAM_ID` is also required; the
adapter will never choose the first workspace silently. Keep writes disabled for read-
only demos. Task reads explicitly include closed work and paginate through the result
set. Before updating a task by id, the adapter fetches it and verifies its documented
`team_id` against the selected workspace.

If ClickUp accepts a write but returns an unexpected task payload, the adapter reports
that the operation may already have succeeded and requires verification in ClickUp
before any retry. Automatic retries and idempotency are intentionally out of scope.

The MCP tool layer does not change when the backend becomes a real vendor API — only
the adapter behind it does.

## Tests and CI

```bash
uv run pytest              # 83 tests: API, MCP handlers, name resolution, ClickUp safety/error paths
uv run ruff check .        # lint
uv run ruff format --check .
```

Tool-handler tests run against the real FastAPI app in-process (httpx ASGI transport +
seeded in-memory SQLite) — no server or network needed. ClickUp tests use an in-memory
fake of the ClickUp v2 API. CI runs the same three commands on every pull request and every push to
main.

## How this was built

This project was built by directing AI coding agents; the split below is deliberate.

**What I decided**

- The scope and architecture: a synthetic platform first, the MCP server talking to it
  only over HTTP, and a real vendor adapter only after the tool surface was stable.
- The tool-design rules the agents had to follow: descriptions written for a model,
  names resolved to ids, ambiguity returned as an error listing candidates, write tools
  returning the changed record.
- The safety posture for the vendor adapter: ClickUp writes off by default and no
  silent choice of workspace.
- Change discipline: every code change landed as a branch and pull request with CI.

**What AI tools generated**

- Claude Code (cloud sessions, June 2026) wrote the platform API, seed data, MCP
  server, ClickUp adapter, tests, CI workflow, and DEMO.md from phase-by-phase briefs.
- OpenAI Codex (September 2026) wrote the ClickUp safety hardening, SECURITY.md, and
  the input-validation fixes for task updates and blank selectors.
- Claude Code (September 2026) assembled this public snapshot and wrote
  `scripts/demo_loop.py`.

**What was verified**

- The full test suite (83 tests) and ruff lint/format checks pass locally; CI reruns them on every pull request and every push to main.
- The demo transcript above is real output (excerpted) from running `scripts/demo_loop.py`
  against a freshly seeded platform.
- The published tree was checked for secrets, real personal data, and employer-specific
  content before release. It is a fresh single-commit history; the development history
  lives in a private repository.

## Known limitations

- The platform API has no authentication and is meant for `127.0.0.1` only.
- The demo replay script sends fixed tool calls; it shows the MCP and API behavior, not
  a model's tool choice. A live model run follows [DEMO.md](DEMO.md).
- The ClickUp adapter has not been exercised against a live ClickUp workspace; it was built
  from ClickUp's v2 API documentation and is verified only against an in-memory fake.
- ClickUp writes have no automatic retry or idempotency key; an ambiguous write result
  must be checked in ClickUp before retrying.
- Not production-ready: a real deployment would need platform authentication and
  authorization, scoped OAuth, managed secrets, write approvals, durable audit logging,
  rate-limit handling, and monitoring.

## License

[MIT](LICENSE)
