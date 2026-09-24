# ops-platform-mcp-demo

[![CI](https://github.com/ericthlai/ops-platform-mcp-demo/actions/workflows/ci.yml/badge.svg)](https://github.com/ericthlai/ops-platform-mcp-demo/actions/workflows/ci.yml)

An **MCP server** that lets an AI assistant read and write business records in plain
language — look up people, create and move tasks, log time, pull utilization — through
a synthetic operations platform (FastAPI + SQLite) and an optional, safe-by-default
adapter for the real ClickUp API.

Writes need a person: the assistant's write tools file change requests, and nothing
changes until a human approves one with a review CLI the assistant has no tool for.
Every write, request, and decision lands in a durable audit log, and 27 scenario evals
check the tool surface — replayed in CI and run against a live model
([results](evals/RESULTS.md)).

## Demo

Output of [`scripts/demo_loop.py`](scripts/demo_loop.py) on a fresh seed (2026-09-23).
Each prompt, taken from the [DEMO.md](DEMO.md) script, is paired with the tool call a
model makes for it and sent through the real MCP server over stdio; the arrow lines are
the server's actual responses. The `$` lines are the human reviewer using the review CLI
(excerpt: some steps and audit rows omitted, long lines wrapped).

```text
connected: 9 MCP tools, approval mode on

> "Create a task on the Atlas project to refresh the KPI deck, assign it to Tessa, due next Friday"
  create_task(project='Atlas', title='Refresh the KPI deck', assignee='Tessa', due_date='2026-09-25')
  -> change request #1 pending: Create task 'Refresh the KPI deck' on Atlas KPI Dashboard,
     assigned to Tessa Morgan, due 2026-09-25

[reviewer, outside the assistant: see what is waiting, then approve it]
  $ python scripts/review_changes.py approve 1
    approved #1: Create task 'Refresh the KPI deck' on Atlas KPI Dashboard, assigned to
    Tessa Morgan, due 2026-09-25 -> task 21 (status todo)

> "Did the KPI deck task go through?"
  get_change_request(change_request_id=1)
  -> change request #1 approved by ops-lead: task 21 'Refresh the KPI deck' status=todo

> "Mark it in progress"
  update_task_status(task_id='21', status='in_progress')
  -> change request #2 pending: Update task 21 'Refresh the KPI deck': status todo -> in_progress

> "Actually, mark it done"
  update_task_status(task_id='21', status='done')
  -> change request #3 pending: Update task 21 'Refresh the KPI deck': status todo -> done

[reviewer, outside the assistant: approve both; the second was submitted against the old status]
  $ python scripts/review_changes.py approve 2
    approved #2: Update task 21 'Refresh the KPI deck': status todo -> in_progress
    -> task 21 (status in_progress)
  $ python scripts/review_changes.py approve 3
    error: HTTP 409: Task 21 changed after this request was submitted (status 'todo' ->
    'in_progress'). Nothing was applied; submit a new request if the change is still wanted.

> "Mark task 999 as done"
  update_task_status(task_id='999', status='done')
  -> ERROR  Error executing tool update_task_status: Platform API error 404: Task 999 not found

[reviewer, outside the assistant: read the audit trail]
  $ python scripts/review_changes.py audit --limit 30
    #1    2026-09-24T04:35:09+00:00  mcp-agent     pending   create_task  request #1
    #2    2026-09-24T04:35:09+00:00  ops-lead      approved  create_task  task 21  request #1
    #3    2026-09-24T04:35:09+00:00  mcp-agent     pending   update_task_status  task 21  request #2
    #4    2026-09-24T04:35:09+00:00  mcp-agent     pending   update_task_status  task 21  request #3
    #5    2026-09-24T04:35:09+00:00  ops-lead      approved  update_task  task 21  request #2
    #6    2026-09-24T04:35:10+00:00  ops-lead      stale     update_task  task 21  request #3
    ...
    #11   2026-09-24T04:35:10+00:00  mcp-agent     error     update_task_status
         Platform API error 404: Task 999 not found
```

Name fragments ("Tessa", "Atlas") resolve to records; unknown ids and ambiguous names
come back as errors the model can act on instead of silent guesses. The full run —
including a rejected time entry, the 30-hour validation error, and the ambiguous
project name — is reproducible with the commands in [Quickstart](#quickstart).

## What it is

```
FastAPI "platform" (SQLite + seed)  ◄─HTTP─►  MCP server (stdio)  ◄─►  Claude Code / any MCP client
  │  change requests + audit log                    │
  ▲                                                 └─ optional: ClickUp API adapter for task tools
  └─HTTP── review CLI (a person approves or rejects)
```

- **Platform API** — a small operations system of record: employees, projects, tasks,
  time entries, plus budget and utilization reports. All data is synthetic.
- **MCP server** — nine tools whose descriptions are written for a model. The server
  talks to the platform only over HTTP, the way a connector wraps a vendor API.
- **Human approval** — write tools submit change requests. A person approves or rejects
  them with `scripts/review_changes.py`, which calls platform routes that no MCP tool
  can reach. Approval re-checks the record and refuses requests that went stale.
- **Audit log** — every applied write, change request, review decision, and failed
  write-tool call is a row in the platform database: who, which tool, arguments,
  before/after, outcome, and a UTC timestamp.
- **Scenario evals** — 27 natural-language requests with expected tool calls and
  outcomes, replayed through the real server in CI and scored against a live model.
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
people, clients, projects, and time entries are fictional. Re-seeding also clears
change requests and the audit log.

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
> has change request 1 been approved? · log 3 hours for Marcus on Orion today ·
> pull this week's utilization report

Approve or reject what it submits from another terminal:

```bash
uv run python scripts/review_changes.py list
uv run python scripts/review_changes.py approve 1 --reviewer ops-lead
uv run python scripts/review_changes.py reject 2 --reason "Quartz is on hold"
uv run python scripts/review_changes.py audit
```

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
| `get_change_request(change_request_id)` | read | pending / approved (with the record written) / rejected (with reason) / stale / failed |
| `create_task(project, title, assignee?, due_date?)` | write | submits a change request; returns it as `pending` |
| `update_task_status(task_id, status)` | write | todo / in_progress / done; submits a change request |
| `log_time(employee, project, date, hours, note?)` | write | submits a change request |

Tools accept human-friendly names where reasonable and resolve them to ids internally;
ambiguous or unknown names return errors that list the candidates so the model can
self-correct. Write requests are validated when submitted, so a bad id or an invalid
value fails immediately instead of waiting in the queue.

### Approvals and audit

The design goal is that the assistant cannot approve its own changes:

- **Separate path for decisions.** Change requests are submitted and read at
  `/change-requests`; approve and reject live under `/admin/change-requests/…`. The MCP
  server has no tool and no code path for the `/admin` routes (a test checks both), so
  an assistant limited to the MCP tools can ask for a change but never make it happen.
- **Four-eyes check.** The platform refuses a decision whose reviewer name matches the
  submitter (`mcp-agent`).
- **Stale requests.** An update snapshots the task when submitted. If the task changed
  before approval — another approved request, a direct edit — approval is refused with
  the fields that changed, and the request is marked `stale`.
- **One transaction.** A write and its audit row commit together, so a change cannot
  land without its audit record. Failed write-tool calls, which never reach a write,
  are reported by the MCP server as `error` rows with the arguments the model sent.
- **Turning it off.** `OPS_REQUIRE_APPROVAL=false` restores direct writes (the original
  behavior); those writes are still audited as `applied`.

Browse the log with `review_changes.py audit` or `GET /audit-events`. There is no route
to edit or delete events. The boundary is the tool surface, not authentication — see
[Known limitations](#known-limitations).

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
The other six tools (employees, hours, time, utilization, change requests) always use
the platform.

Copy the environment template, then ask `uv` to load it explicitly:

```bash
cp .env.example .env
# Edit .env, then run the MCP server directly for a smoke test:
uv run --env-file .env python -m mcp_server.server
```

`uv` does not load `.env` implicitly. When ClickUp mode is launched through an MCP
client, add `--env-file /absolute/path/to/.env` to the `uv run` arguments or inject the
same variables through the client's environment configuration.

ClickUp reads are available once a token is configured. The approval queue can only
apply platform changes, so `create_task` and `update_task_status` fail closed in ClickUp
mode until both `OPS_REQUIRE_APPROVAL=false` and `CLICKUP_WRITES_ENABLED=true` are set;
successful ClickUp writes are then recorded in the platform audit log as `external`.
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

## Evals

[`evals/scenarios.yaml`](evals/scenarios.yaml) holds 27 requests a user might type —
lookups, creates, updates, time logging, budget and utilization questions, change-request
status, ambiguous names, missing records, invalid input, and requests the assistant must
not carry out — each with the reference tool calls and the outcome they must produce.

- **Deterministic layer (CI, no model).** `tests/test_eval_scenarios.py` sends each
  scenario's reference calls through the MCP protocol into the real platform on a fresh
  seed and checks the outcome: success, pending approval, a candidate list, or the
  expected 404/422/ambiguity error.
- **Live-model layer (local, not CI).** `uv run python -m evals.run_model_eval` gives each
  prompt to Claude Code in headless mode with only these MCP tools allowed, then scores
  tool selection, key arguments, whether any write went through when it should not
  have, and whether the reply says the change is pending approval. The 2026-09-23 run
  passed 27/27; [evals/RESULTS.md](evals/RESULTS.md) has the per-scenario table and what
  that score does not show.

## Tests and CI

```bash
uv run pytest              # 216 tests: API, approvals, audit, MCP handlers, review CLI, ClickUp, scenario evals
uv run ruff check .        # lint
uv run ruff format --check .
```

Tool-handler tests run against the real FastAPI app in-process (httpx ASGI transport +
seeded in-memory SQLite) — no server or network needed. ClickUp tests use an in-memory
fake of the ClickUp v2 API. CI runs lint, the format check, the test suite, and the
deterministic scenario evals as a separate step on every pull request and every push to
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
- Claude Code (September 2026) built the approval workflow, the audit log, the review
  CLI, and the scenario evals, ran the live-model eval, and updated the demo and docs.

**What was verified**

- The full test suite (216 tests) and ruff lint/format checks pass locally; CI reruns them on every pull request and every push to main.
- The demo transcript above is real output (excerpted) from running `scripts/demo_loop.py`
  against a freshly seeded platform.
- The live-model eval results are from an actual run; the model identifiers, per-scenario
  calls, and replies are recorded in `evals/RESULTS.md` and `evals/last_run.json`.
- The published tree was checked for secrets, real personal data, and employer-specific
  content before release. It was first published as a fresh single-commit history; the
  earlier development history lives in a private repository.

## Known limitations

- The platform API has no authentication and is meant for `127.0.0.1` only. The
  approval boundary is the MCP tool surface: anything else that can reach the API —
  including an assistant that also has shell access and runs the review CLI — can
  approve requests. Keep shell tools behind permission prompts when an assistant is
  connected.
- Actor and reviewer names are self-reported headers and fields, and the audit log is a
  local SQLite table, not tamper-evident storage.
- Change requests do not expire; a stale check runs only when someone approves. The
  approval queue cannot hold ClickUp writes.
- The demo replay script sends fixed tool calls; it shows the MCP and API behavior, not
  a model's tool choice. The live-model eval covers tool choice, but it is a single run
  of 27 scenarios on one model.
- The ClickUp adapter has not been exercised against a live ClickUp workspace; it was built
  from ClickUp's v2 API documentation and is verified only against an in-memory fake.
- ClickUp writes have no automatic retry or idempotency key; an ambiguous write result
  must be checked in ClickUp before retrying.
- Not production-ready: a real deployment would need platform authentication and
  authorization (including authenticated reviewers), scoped OAuth, managed secrets,
  tamper-evident audit storage, rate-limit handling, and monitoring.

## License

[MIT](LICENSE)
