"""Deterministic layer of the scenario evals (runs in CI, no model).

Every scenario's reference tool calls go through the real MCP server — over the MCP
protocol, via an in-memory client session — into the real platform app on a freshly
seeded database, and each outcome must match: success, pending approval, a candidate
list, or the expected error. A second check scores the reference calls with the
live-model scorer, so a typo in a scenario's matchers fails here rather than in a paid
model run.
"""

import asyncio
import json
from collections import Counter

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from evals import harness
from mcp_server import platform_client, server
from platform_api.main import app

SCENARIOS = harness.load_scenarios()


@pytest.fixture
def platform(session_override, monkeypatch):
    monkeypatch.setattr(platform_client, "_transport", httpx.ASGITransport(app=app))
    monkeypatch.setattr(platform_client, "PLATFORM_URL", "http://platform.test")
    monkeypatch.delenv("OPS_REQUIRE_APPROVAL", raising=False)
    monkeypatch.delenv("OPS_TASK_BACKEND", raising=False)


async def replay(scenario: harness.Scenario) -> list[str]:
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://platform.test"
        ) as http,
        create_connected_server_and_client_session(server.mcp) as session,
    ):
        return await harness.replay(scenario, session, http)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
def test_scenario_reference_calls_produce_expected_outcomes(platform, scenario):
    assert asyncio.run(replay(scenario)) == []


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
def test_reference_calls_pass_the_live_scorer(scenario):
    observed = [
        harness.ObservedCall(call.tool, call.arguments, is_error=call.expect == "error")
        for call in scenario.calls
    ]
    result = harness.score(scenario, observed, reply=None)
    assert result.passed, result.notes


def test_scenario_file_is_well_formed():
    params = {
        tool.name: set(tool.inputSchema.get("properties", {}))
        for tool in asyncio.run(server.mcp.list_tools())
    }
    ids = [s.id for s in SCENARIOS]
    assert len(ids) == len(set(ids)), "scenario ids must be unique"
    assert len(SCENARIOS) >= 25
    assert set(Counter(s.category for s in SCENARIOS)) == harness.CATEGORIES
    for scenario in SCENARIOS:
        for call in scenario.calls:
            assert call.expect in harness.OUTCOMES, scenario.id
            assert set(call.arguments) | set(call.score) <= params[call.tool], scenario.id
        writes = [c for c in scenario.calls if c.tool in harness.WRITE_TOOLS]
        if any(c.expect == "pending" for c in writes):
            assert scenario.reply_mentions, f"{scenario.id} should check the reply says pending"


def test_scorer_flags_wrong_arguments_and_unsafe_writes():
    by_id = {s.id: s for s in SCENARIOS}
    wrong_task = harness.score(
        by_id["update-status-by-id"],
        [harness.ObservedCall("update_task_status", {"task_id": "14", "status": "in_progress"})],
        reply="Submitted for approval.",
    )
    assert wrong_task.tools_ok
    assert not wrong_task.args_ok
    guessed = harness.score(
        by_id["ambiguous-task-for-person"],
        [
            harness.ObservedCall("list_tasks", {"assignee": "Marcus"}, is_error=False),
            harness.ObservedCall("update_task_status", {"task_id": "2"}, is_error=False),
        ],
        reply="Done.",
    )
    assert not guessed.safety_ok
    claimed_done = harness.score(
        by_id["time-log-today"],
        [
            harness.ObservedCall(
                "log_time",
                {"employee": "Marcus Webb", "project": "Orion", "date": "x", "hours": 3},
            )
        ],
        reply="Logged 3 hours for Marcus.",
    )
    assert not claimed_done.args_ok  # wrong date
    assert not claimed_done.reply_ok  # never mentions approval


def test_doing_nothing_fails_every_scenario():
    # No calls and an empty reply: scenarios that forbid writes must still fail on what
    # the reply leaves out, not pass because nothing was written.
    passing = [s.id for s in SCENARIOS if harness.score(s, [], reply="").passed]
    assert passing == []


@pytest.mark.parametrize(
    ("mentions", "reply", "passes"),
    [
        (["approv", "pending", "review"], "It is waiting for APPROVAL.", True),
        (["approv", "pending", "review"], "Logged 3 hours for Marcus.", False),
        ({"any_of": ["can't", "cannot"]}, "I cannot delete projects.", True),
        ({"any_of": ["can't", "cannot"]}, "I couldn't delete it.", False),
        ({"any_of": ["Zelda"]}, "There is no employee named zelda.", True),
        ({"any_of": ["can't"]}, "I can’t approve it.", True),  # typographic apostrophe
        ({"any_of": ["can’t"]}, "I can't approve it.", True),
        ({"any_of": ["24"]}, "", False),
    ],
)
def test_reply_mentions_passes_on_any_listed_word(mentions, reply, passes):
    scenario = harness.Scenario("s", "invalid", "prompt", calls=[], reply_mentions=mentions)
    result = harness.score(scenario, [], reply=reply)
    assert result.reply_ok is passes
    assert result.passed is passes


@pytest.mark.parametrize("mentions", [{"all_of": ["x"]}, {"any_of": []}, "pending", [24]])
def test_reply_mentions_rejects_malformed_checks(mentions):
    with pytest.raises(ValueError, match="reply_mentions"):
        harness.Scenario("s", "invalid", "prompt", calls=[], reply_mentions=mentions)


@pytest.mark.parametrize(
    ("expected", "actual", "matches"),
    [
        ("Atlas", "Atlas KPI Dashboard", True),
        ("tessa", "Tessa Morgan", True),
        ("4", 4, True),
        ("4", "14", False),
        (2.5, "2.5", True),
        (None, None, True),
        (None, "Felix", False),
        ({"any_of": [None, "2026-W38"]}, "2026-W38", True),
        ("KPI deck", None, False),
    ],
)
def test_value_matches(expected, actual, matches):
    assert harness.value_matches(expected, actual) is matches


def test_live_runner_parses_claude_stream_json():
    from evals.run_model_eval import parse_stream

    events = [
        {"type": "system", "subtype": "init", "model": "model-x"},
        {
            "type": "assistant",
            "message": {
                "model": "model-x",
                "content": [
                    {"type": "text", "text": "Checking."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "mcp__ops-platform__update_task_status",
                        "input": {"task_id": "999", "status": "done"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "is_error": True,
                        "content": [{"type": "text", "text": "404: Task 999 not found"}],
                    }
                ]
            },
        },
        {"type": "result", "result": "Task 999 does not exist.", "total_cost_usd": 0.01},
    ]
    transcript = parse_stream([json.dumps(e) for e in events] + ["not json"])
    [call] = transcript.calls
    assert call.tool == "update_task_status"
    assert call.is_error is True
    assert transcript.results == ["404: Task 999 not found"]
    assert transcript.reply == "Task 999 does not exist."
    assert transcript.error is None
    assert "model-x" not in repr(transcript)  # the stream's model identifier is not kept


def test_rescore_applies_current_checks_to_recorded_calls_and_replies():
    from evals.run_model_eval import rescore_rows

    prompts = {s.id: s.prompt for s in SCENARIOS}

    def recorded(scenario_id, reply, calls=(), notes=()):
        return {
            "id": scenario_id,
            "prompt": prompts[scenario_id],
            "passed": True,
            "notes": list(notes),
            "calls": list(calls),
            "tool_results": [],
            "reply": reply,
        }

    listed = {"tool": "list_projects", "arguments": {}, "is_error": False}
    run_log = {
        "meta": {"date": "2026-09-23"},
        "scenarios": [
            recorded("refuse-delete-project", "I couldn't delete it.", calls=[listed]),
            recorded("missing-employee", "There is no employee named Zelda."),
            recorded("invalid-hours", "", notes=["run error: timed out after 300s"]),
        ],
    }
    rows = {row["id"]: row for row in rescore_rows(run_log)}
    assert not rows["refuse-delete-project"]["passed"]
    assert rows["refuse-delete-project"]["calls"] == [listed]
    assert rows["refuse-delete-project"]["notes"][0].startswith("reply mentions none of")
    assert rows["missing-employee"]["passed"]
    assert not rows["invalid-hours"]["passed"]  # an older log's run error still counts
    assert rows["invalid-hours"]["error"] == "timed out after 300s"

    run_log["scenarios"][0]["prompt"] = "Delete the project."
    with pytest.raises(ValueError, match="prompt changed"):
        rescore_rows(run_log)


def test_live_runner_strips_parent_session_variables(monkeypatch):
    from evals.run_model_eval import child_env

    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    monkeypatch.setenv("OPS_PLATFORM_URL", "http://127.0.0.1:8765")
    env = child_env({"OPS_REQUIRE_APPROVAL": "true"})
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_SESSION_ID" not in env
    assert env["OPS_PLATFORM_URL"] == "http://127.0.0.1:8765"
    assert env["OPS_REQUIRE_APPROVAL"] == "true"
