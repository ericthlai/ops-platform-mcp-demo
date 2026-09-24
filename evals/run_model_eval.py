"""Live-model layer of the scenario evals (not run in CI).

Sends each scenario prompt to a real model through Claude Code in headless mode, with
only the ops-platform MCP tools available, and scores which tools it called and with
which key arguments (see harness.score). Uses the local Claude Code login, so no API key
is needed. Each scenario starts from a freshly seeded platform on a temporary database;
the platform, the MCP server, and the model all run locally.

    uv run python -m evals.run_model_eval                 # all scenarios
    uv run python -m evals.run_model_eval --only time-log-today invalid-hours
    uv run python -m evals.run_model_eval --model <alias>  # any `claude --model` value

Writes evals/RESULTS.md and evals/last_run.json (tool calls, results, and final replies).
A full run makes one headless Claude Code call per scenario; --budget caps each call.
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlmodel import create_engine

from evals import harness
from platform_api.seed import seed

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_NAME = "ops-platform"
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"
TOOLS = [
    "list_employees",
    "list_projects",
    "list_tasks",
    "get_project_hours",
    "utilization_report",
    "get_change_request",
    "create_task",
    "update_task_status",
    "log_time",
]
SYSTEM_PROMPT = (
    "You are an operations assistant for a small consulting firm. You work through the "
    "ops-platform MCP tools. Reply to the user in English. Today's date is {today}."
)
# Variables that tie a process to a parent Claude Code session; a nested headless run
# must not inherit them.
PARENT_SESSION_VARS = ("CLAUDECODE", "CLAUDE_CODE_", "CLAUDE_PID", "CLAUDE_EFFORT")
# Report wording when --model is not given. The runner never records the identifier the
# model reports about itself.
DEFAULT_MODEL = "Claude Code's default at run time (identifier not recorded)"


@dataclass
class Transcript:
    calls: list[harness.ObservedCall] = field(default_factory=list)
    results: list[str] = field(default_factory=list)
    reply: str = ""
    cost_usd: float = 0.0
    error: str | None = None


def claude_command(prompt: str, mcp_config: Path | str, today: str, args) -> list[str]:
    command = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--mcp-config",
        str(mcp_config),
        "--strict-mcp-config",
        "--tools",
        "",
        "--allowedTools",
        ",".join(TOOL_PREFIX + tool for tool in TOOLS),
        "--permission-prompts",
        "none",
        "--no-session-persistence",
        "--setting-sources",
        "",
        "--append-system-prompt",
        SYSTEM_PROMPT.format(today=today),
        "--max-budget-usd",
        str(args.budget),
    ]
    if args.model:
        command += ["--model", args.model]
    return command


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(PARENT_SESSION_VARS)}
    return {**env, **(extra or {})}


def parse_stream(lines: list[str]) -> Transcript:
    transcript = Transcript()
    pending: dict[str, harness.ObservedCall] = {}
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    name = block["name"].removeprefix(TOOL_PREFIX)
                    call = harness.ObservedCall(name, block.get("input") or {})
                    transcript.calls.append(call)
                    pending[block["id"]] = call
        elif kind == "user":
            for block in event.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    call = pending.get(block.get("tool_use_id"))
                    if call is not None:
                        call.is_error = block.get("is_error") is True
                        content = block.get("content")
                        text = (
                            content
                            if isinstance(content, str)
                            else " ".join(c.get("text", "") for c in content or [])
                        )
                        transcript.results.append(text)
        elif kind == "result":
            transcript.reply = event.get("result") or ""
            transcript.cost_usd = event.get("total_cost_usd") or 0.0
            if event.get("is_error"):
                transcript.error = event.get("subtype", "error")
    return transcript


def reseed(db_url: str) -> None:
    engine = create_engine(db_url)
    seed(engine)
    engine.dispose()  # release the file so the temp directory can be removed on Windows


async def prepare(scenario: harness.Scenario, db_url: str, platform_url: str) -> None:
    """Reseed the platform and run the scenario's setup through the real MCP server."""
    reseed(db_url)
    if not scenario.setup:
        return
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.server"],
        env=child_env({"OPS_PLATFORM_URL": platform_url, "OPS_REQUIRE_APPROVAL": "true"}),
    )
    async with (
        open(os.devnull, "w") as quiet,  # the server logs every request to stderr
        stdio_client(params, errlog=quiet) as (read, write),
        ClientSession(read, write) as session,
        httpx.AsyncClient(base_url=platform_url, timeout=10.0) as http,
    ):
        await session.initialize()
        await harness.run_setup(scenario, session, http)


def run_model(scenario, mcp_config: Path, workdir: Path, today: str, args) -> Transcript:
    try:
        completed = subprocess.run(
            claude_command(scenario.prompt, mcp_config, today, args),
            cwd=workdir,
            env=child_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=args.timeout,
        )
    except subprocess.TimeoutExpired:
        return Transcript(error=f"timed out after {args.timeout}s")
    transcript = parse_stream(completed.stdout.splitlines())
    if completed.returncode != 0 and transcript.error is None:
        transcript.error = f"claude exited {completed.returncode}: {completed.stderr[-300:]}"
    return transcript


def start_platform(db_url: str, port: int) -> subprocess.Popen:
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "platform_api.main:app", "--port", str(port)],
        cwd=REPO_ROOT,
        env=child_env({"DATABASE_URL": db_url}),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/employees", timeout=1).status_code == 200:
                return process
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    process.terminate()
    raise SystemExit(f"platform did not start on port {port}")


def describe_call(call: harness.ObservedCall) -> str:
    arguments = ", ".join(f"{k}={v!r}" for k, v in call.arguments.items())
    suffix = " [error]" if call.is_error else ""
    return f"`{call.tool}({arguments})`{suffix}"


def sanitize(text: str, *paths: Path) -> str:
    for path in paths:
        for form in {str(path), path.as_posix()}:
            text = text.replace(form, "<local path>")
    return text


def write_report(rows: list[dict], meta: dict, output: Path) -> None:
    total = len(rows)
    passed = sum(row["passed"] for row in rows)

    def tally(check: str, applies: str | None = None) -> str:
        relevant = [row for row in rows if applies is None or row[applies]]
        return f"{sum(row[check] for row in relevant)}/{len(relevant)}"

    by_category: dict[str, list[bool]] = {}
    for row in rows:
        by_category.setdefault(row["category"], []).append(row["passed"])
    model = f"`{meta['model_flag'].strip()}`" if meta["model_flag"] else DEFAULT_MODEL
    lines = [
        "# Live-model eval results",
        "",
        f"- Date: {meta['date']}",
        f"- Model: {model}",
        f"- Runner: Claude Code {meta['claude_version']} in headless mode "
        "(`claude -p`), only the nine ops-platform MCP tools allowed",
        f"- Scenarios: {total} from `evals/scenarios.yaml`, one fresh platform seed each, "
        "approval mode on",
        f"- **Overall: {passed}/{total} scenarios pass ({passed / total:.0%})**",
        "",
        "| Check | Scenarios passing |",
        "|---|---|",
        f"| Tool selection: every required tool called | {tally('tools_ok')} |",
        f"| Key arguments: required calls match | {tally('args_ok')} |",
        f"| No write went through (refusal, ambiguous, missing, invalid) "
        f"| {tally('safety_ok', 'safety_applies')} |",
        f"| Reply says the change is pending approval (write requests) "
        f"| {tally('reply_ok', 'reply_applies')} |",
        "",
        "By category: "
        + ", ".join(f"{cat} {sum(v)}/{len(v)}" for cat, v in sorted(by_category.items())),
        "",
        "## Per scenario",
        "",
        "| Scenario | Category | Result | Tool calls made | Notes |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        calls = "<br>".join(row["calls_md"]) or "(none)"
        notes = "; ".join(row["notes"]).replace("|", "\\|") or ""
        verdict = "pass" if row["passed"] else "**fail**"
        lines.append(f"| `{row['id']}` | {row['category']} | {verdict} | {calls} | {notes} |")
    lines += [
        "",
        "## How it was run",
        "",
        "```bash",
        "uv run python -m evals.run_model_eval" + meta["model_flag"],
        "```",
        "",
        "Each scenario is one headless Claude Code call of this shape (temporary paths "
        "shown as placeholders):",
        "",
        "```bash",
        meta["command"],
        "```",
        "",
        "Scoring is in `evals/harness.py` (`score`). Replies and tool results for this run "
        "are in `evals/last_run.json`.",
        "",
    ]
    output.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", help="passed to `claude --model`; default: Claude Code's")
    parser.add_argument("--only", nargs="+", metavar="ID", help="run only these scenario ids")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--timeout", type=int, default=300, help="seconds per scenario")
    parser.add_argument("--budget", type=float, default=0.5, help="max USD per scenario")
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "evals" / "RESULTS.md")
    args = parser.parse_args()
    if shutil.which("claude") is None:
        raise SystemExit("Claude Code (`claude`) is not on PATH; the live eval needs it.")
    sys.stdout.reconfigure(encoding="utf-8")

    today = date.today()
    scenarios = harness.load_scenarios(today=today)
    if args.only:
        scenarios = [s for s in scenarios if s.id in set(args.only)]
    claude_version = subprocess.run(
        ["claude", "--version"], capture_output=True, text=True, env=child_env()
    ).stdout.split()[0]

    with tempfile.TemporaryDirectory(prefix="ops-eval-", ignore_cleanup_errors=True) as tmp:
        tmpdir = Path(tmp)
        workdir = tmpdir / "workdir"
        workdir.mkdir()
        db_url = f"sqlite:///{(tmpdir / 'eval.db').as_posix()}"
        platform_url = f"http://127.0.0.1:{args.port}"
        mcp_config = tmpdir / "mcp.json"
        server_env = {"OPS_PLATFORM_URL": platform_url, "OPS_REQUIRE_APPROVAL": "true"}
        mcp_config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        SERVER_NAME: {
                            "command": sys.executable,
                            "args": ["-m", "mcp_server.server"],
                            "env": server_env,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        reseed(db_url)
        platform = start_platform(db_url, args.port)
        rows, cost = [], 0.0
        try:
            for scenario in scenarios:
                asyncio.run(prepare(scenario, db_url, platform_url))
                transcript = run_model(scenario, mcp_config, workdir, today.isoformat(), args)
                result = harness.score(scenario, transcript.calls, transcript.reply)
                notes = list(result.notes)
                if transcript.error:
                    notes.insert(0, f"run error: {transcript.error}")
                passed = result.passed and transcript.error is None
                cost += transcript.cost_usd
                verdict = "PASS" if passed else "FAIL"
                print(f"{verdict}  {scenario.id}  {'; '.join(notes)}", flush=True)
                rows.append(
                    {
                        "id": scenario.id,
                        "category": scenario.category,
                        "prompt": scenario.prompt,
                        "passed": passed,
                        "tools_ok": result.tools_ok,
                        "args_ok": result.args_ok,
                        "safety_ok": result.safety_ok,
                        "reply_ok": result.reply_ok,
                        "safety_applies": scenario.no_successful_writes,
                        "reply_applies": bool(scenario.reply_mentions),
                        "notes": notes,
                        "calls": [vars(c) for c in transcript.calls],
                        "calls_md": [describe_call(c) for c in transcript.calls],
                        "tool_results": transcript.results,
                        "reply": transcript.reply,
                    }
                )
        finally:
            platform.terminate()
            platform.wait(timeout=10)

        command = " ".join(
            json.dumps(part) if (" " in part or "<" in part or not part) else part
            for part in claude_command("<prompt>", "<tmp>/mcp.json", "<today>", args)
        )
        meta = {
            "date": today.isoformat(),
            "claude_version": claude_version,
            "model_flag": f" --model {args.model}" if args.model else "",
            "command": command,
        }
        write_report(rows, meta, args.output)
        scenarios_log = [{k: v for k, v in row.items() if k != "calls_md"} for row in rows]
        run_log = {"meta": {**meta, "total_cost_usd": round(cost, 4)}, "scenarios": scenarios_log}
        text = sanitize(json.dumps(run_log, indent=2, ensure_ascii=False), tmpdir, Path.home())
        args.output.with_name("last_run.json").write_text(text, encoding="utf-8", newline="\n")
    passed = sum(row["passed"] for row in rows)
    print(f"\n{passed}/{len(rows)} scenarios pass; approx cost ${cost:.2f}; see {args.output}")


if __name__ == "__main__":
    main()
