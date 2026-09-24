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
- `.env` and local SQLite databases are ignored by Git, but operators remain
  responsible for secret storage, logs, shell history, and backups.

## Production use

This repository is a portfolio-quality integration rehearsal, not a production access
gateway. Production adoption would additionally require authentication and
authorization for the platform API, managed secrets, scoped OAuth where available,
write approvals, idempotency, durable audit logs, rate-limit handling, monitoring, and
a deployment-specific threat model.
