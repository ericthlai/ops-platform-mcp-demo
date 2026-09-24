# Security policy

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use GitHub's
**Security → Report a vulnerability** workflow. If private reporting is unavailable,
contact the maintainer through the GitHub profile without including exploit details
in the initial message.

Include the affected revision, reproduction steps, impact, and any suggested
mitigation. Please coordinate public disclosure with the maintainer so a fix and any
necessary credential rotation can be completed first.

## Security boundaries

- The FastAPI platform is an unauthenticated, localhost-only demonstration service.
  Its seeded people, clients, projects, and time entries are synthetic. Do not bind it
  to a public interface or use it with production data.
- Keep `CLICKUP_API_TOKEN` out of source control and use the least-privileged account
  available. Rotate the token immediately if it is exposed.
- Real ClickUp writes are disabled unless `CLICKUP_WRITES_ENABLED=true` is explicitly
  set. Reads remain enabled. A token with multiple workspaces also requires an explicit
  `CLICKUP_TEAM_ID`; the adapter will not guess a target. Status updates fetch the task
  first and verify its `team_id` against the selected workspace before sending a write.
- Platform writes made through the MCP tools need human approval by default. The MCP
  server can submit and read change requests but has no tool or code path to the
  `/admin` review routes; a person decides with `scripts/review_changes.py`. This
  boundary is the tool surface, not authentication: anything that can reach the API on
  localhost — including an assistant that also has shell access — can call the review
  routes. Keep shell and other non-MCP tools behind permission prompts when an
  assistant is connected.
- The approval queue can only apply platform changes. ClickUp writes are blocked while
  approval mode is on and require both `OPS_REQUIRE_APPROVAL=false` and
  `CLICKUP_WRITES_ENABLED=true`.
- The audit log (`audit_events` table) has no API route to edit or delete events, but it
  is a local SQLite file, not tamper-evident storage, and actor names are self-reported
  through request headers.
- `.env` and local SQLite databases are ignored by Git, but operators remain
  responsible for secret storage, logs, shell history, and backups.

## Production use

This repository is a portfolio-quality integration rehearsal, not a production access
gateway. Its approval queue and audit log show the workflow, not a hardened control.
Production adoption would additionally require authentication and authorization for the
platform API (including authenticated reviewers for the approval routes), managed
secrets, scoped OAuth where available, idempotency, tamper-evident audit storage,
rate-limit handling, monitoring, and a deployment-specific threat model.
