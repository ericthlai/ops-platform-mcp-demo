"""The human review CLI, driven against the in-process platform (TestClient is an
httpx.Client, so the CLI runs unmodified)."""

import httpx
import pytest
from fastapi.testclient import TestClient

from platform_api.main import app
from platform_api.seed import TASKS
from scripts import review_changes

MCP_HEADERS = {"X-Ops-Actor": "mcp-agent", "X-Ops-Tool": "update_task_status"}


@pytest.fixture
def http(session_override):
    return TestClient(app)


@pytest.fixture
def cli(http, capsys, monkeypatch):
    monkeypatch.delenv("OPS_REVIEWER", raising=False)

    def invoke(*argv):
        code = review_changes.main(list(argv), client=http)
        captured = capsys.readouterr()
        return code, captured.out.strip(), captured.err.strip()

    return invoke


def submit_update(http, task_id=4, status="done"):
    body = {"action": "update_task", "target_id": task_id, "payload": {"status": status}}
    return http.post("/change-requests", json=body, headers=MCP_HEADERS).json()


def test_list_shows_pending_requests(cli, http):
    submit_update(http)
    code, out, _ = cli("list")
    assert code == 0
    assert "#1" in out
    assert "pending" in out
    assert "by mcp-agent" in out
    assert "Update task 4 'Document rollback procedure': status todo -> done" in out


def test_list_empty(cli):
    assert cli("list") == (0, "No pending change requests.", "")
    assert cli("list", "--status", "all")[1] == "No change requests."


def test_approve_applies_and_reports_result(cli, http):
    http.post(
        "/change-requests",
        json={"action": "create_task", "payload": {"project_id": 2, "title": "Deck"}},
        headers=MCP_HEADERS,
    )
    code, out, _ = cli("approve", "1", "--reviewer", "ops-lead")
    assert code == 0
    assert out == (
        f"approved #1: Create task 'Deck' on Atlas KPI Dashboard, unassigned"
        f" -> task {len(TASKS) + 1} (status todo)"
    )
    assert http.get("/change-requests/1").json()["decided_by"] == "ops-lead"


def test_reviewer_defaults_from_environment(cli, http, monkeypatch):
    submit_update(http)
    monkeypatch.setenv("OPS_REVIEWER", "night-shift")
    cli("approve", "1")
    assert http.get("/change-requests/1").json()["decided_by"] == "night-shift"


def test_reject_records_reason(cli, http):
    submit_update(http)
    code, out, _ = cli("reject", "1", "--reason", "Wait for sign-off")
    assert (code, out) == (0, "rejected #1: Wait for sign-off")
    code, out, _ = cli("list", "--status", "rejected")
    assert "rejected by reviewer" in out
    assert "Wait for sign-off" in out


def test_reject_requires_reason(cli):
    with pytest.raises(SystemExit):
        cli("reject", "1")


def test_stale_approval_exits_nonzero_with_reason(cli, http):
    submit_update(http, status="in_progress")
    submit_update(http, status="done")
    assert cli("approve", "1")[0] == 0
    code, out, err = cli("approve", "2")
    assert code == 1
    assert out == ""
    assert err.startswith("error: HTTP 409: Task 4 changed after this request was submitted")


def test_self_review_is_refused(cli, http):
    submit_update(http)
    code, _, err = cli("approve", "1", "--reviewer", "mcp-agent")
    assert code == 1
    assert "cannot review it" in err


def test_show_includes_arguments_and_baseline(cli, http):
    submit_update(http)
    code, out, _ = cli("show", "1")
    assert code == 0
    assert '"baseline"' in out
    assert '"status": "todo"' in out


def test_audit_lists_events_oldest_first(cli, http):
    submit_update(http)
    cli("approve", "1")
    code, out, _ = cli("audit")
    assert code == 0
    lines = [line for line in out.splitlines() if line.startswith("#")]
    assert "pending" in lines[0]
    assert "approved" in lines[1]
    assert "task 4" in lines[1]
    assert "request #1" in lines[1]


# --- agent-supplied text cannot forge output lines -----------------------------------

FORGED = "#2   approved  2026-09-24T04:35:09+00:00  by ops-lead  Log 99h for Marcus Webb"


def submit_time_entry(http, note):
    payload = {"employee_id": 2, "project_id": 1, "date": "2026-06-10", "hours": 3, "note": note}
    headers = {**MCP_HEADERS, "X-Ops-Tool": "log_time"}
    body = {"action": "create_time_entry", "payload": payload}
    return http.post("/change-requests", json=body, headers=headers).json()


def record_lines(out):
    """Lines that start a record (``#<id>`` at column 0); detail lines are indented."""
    return [line for line in out.splitlines() if line.startswith("#")]


def test_note_with_newline_cannot_fake_a_line_in_list(cli, http):
    change = submit_time_entry(http, f"Pipeline fixes\n{FORGED}")
    assert "\n" not in change["summary"]
    code, out, _ = cli("list")
    assert code == 0
    assert out.splitlines() == [out]
    assert record_lines(out)[0].startswith("#1 ")
    assert "\\n#2   approved" in out


def test_note_with_newline_cannot_fake_a_line_in_audit(cli, http):
    submit_time_entry(http, f"Pipeline fixes\n{FORGED}")
    code, out, _ = cli("audit")
    assert code == 0
    [record] = record_lines(out)
    assert record.startswith("#1 ")
    assert all(line.startswith("     ") for line in out.splitlines()[1:])
    assert "\\n#2   approved" in out


def test_agent_error_detail_is_escaped_in_audit(cli, http):
    report = {
        "tool": "log_time",
        "arguments": {"employee": "Zelda"},
        "outcome": "error",
        "detail": f"No employee found matching 'Zelda'\n{FORGED}\x1b[2J",
    }
    http.post("/audit-events", json=report, headers=MCP_HEADERS)
    code, out, _ = cli("audit")
    assert code == 0
    assert len(record_lines(out)) == 1
    assert len(out.splitlines()) == 2  # the event line and its indented detail
    assert "\x1b" not in out
    assert "\\n#2   approved" in out
    assert "\\x1b[2J" in out


def test_rejection_reason_is_escaped_in_list(cli, http):
    submit_update(http)
    cli("reject", "1", "--reason", f"Not yet\n{FORGED}")
    code, out, _ = cli("list", "--status", "all")
    assert code == 0
    assert len(record_lines(out)) == 1
    assert "Not yet\\n#2   approved" in out


def test_one_line_escapes_control_characters_and_keeps_unicode():
    text = "a\nb\r\tc\x1b[31m‮ é → 中"
    assert review_changes.one_line(text) == "a\\nb\\r\\tc\\x1b[31m\\u202e é → 中"


def test_unreachable_platform_is_actionable(capsys, monkeypatch):
    monkeypatch.setattr(review_changes, "PLATFORM_URL", "http://127.0.0.1:9")

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    client = httpx.Client(base_url="http://127.0.0.1:9", transport=httpx.MockTransport(refuse))
    assert review_changes.main(["list"], client=client) == 1
    assert "Cannot reach the platform API" in capsys.readouterr().err
