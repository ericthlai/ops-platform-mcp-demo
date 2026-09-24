"""Scenario loading, deterministic replay, and live-model scoring.

Each scenario in scenarios.yaml pairs a natural-language request with the reference tool
calls a good assistant would make and the outcome each call must produce. Two layers use
this module:

- Deterministic (pytest, CI): ``replay`` sends the reference calls through the real MCP
  server and platform and checks each outcome — success, pending approval, candidate
  list, or a specific error. No model is involved.
- Live (evals/run_model_eval.py, not CI): a real model gets the prompt and the MCP
  tools; ``score`` compares the calls it made with the scenario's expectations.

Dates in scenarios are written as placeholders ($today, $yesterday, $next_friday,
$friday_after, $this_week, $last_week) and rendered relative to the run date, matching
the seed's relative dates.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from string import Template
from typing import Any

import httpx
import yaml
from mcp import ClientSession
from mcp.types import CallToolResult

SCENARIOS_PATH = Path(__file__).with_name("scenarios.yaml")
WRITE_TOOLS = frozenset({"create_task", "update_task_status", "log_time"})
CATEGORIES = frozenset(
    {
        "lookup",
        "budget",
        "create",
        "update",
        "time",
        "approval",
        "ambiguous",
        "missing",
        "invalid",
        "refusal",
    }
)
OUTCOMES = frozenset({"ok", "pending", "error"})
EVAL_REVIEWER = "eval-reviewer"


@dataclass
class ExpectedCall:
    tool: str
    arguments: dict[str, Any]
    expect: str
    required: bool = True  # live layer: must the model make this call?
    score: dict[str, Any] = field(default_factory=dict)  # live layer: key-argument matchers
    fields: dict[str, Any] = field(default_factory=dict)  # ok: subset of the result
    contains: list[str] = field(default_factory=list)  # ok: substrings of the result JSON
    payload: dict[str, Any] = field(default_factory=dict)  # pending: subset of queued payload
    error: str | None = None  # error: regex the message must match


@dataclass
class Scenario:
    id: str
    category: str
    prompt: str
    calls: list[ExpectedCall]
    setup: list[dict[str, Any]] = field(default_factory=list)
    no_successful_writes: bool = False
    # live layer: the final reply must contain at least one of these (see mentions_any);
    # written as a plain list or as the explicit {any_of: [...]}, which mean the same
    reply_mentions: list[str] | dict[str, list[str]] = field(default_factory=list)
    absent_tools: str | None = None

    def __post_init__(self) -> None:
        mention_options(self.reply_mentions)  # reject a malformed check at load time


def mention_options(spec: list[str] | dict[str, list[str]]) -> list[str]:
    """The words a ``reply_mentions`` check accepts; a reply passes if it has any one."""
    options = spec
    if isinstance(spec, dict):
        options = spec.get("any_of") if set(spec) == {"any_of"} and spec["any_of"] else None
    if not isinstance(options, list) or not all(isinstance(o, str) and o for o in options):
        raise ValueError(f"reply_mentions must be a list of words or {{any_of: [...]}}: {spec!r}")
    return options


def _fold(text: str) -> str:
    return text.replace("\u2019", "'").casefold()


def mentions_any(reply: str, options: list[str]) -> bool:
    """Case-insensitive substring match against any option. A typographic apostrophe
    (U+2019) counts as an ASCII one on either side, so "can't" matches "can’t"."""
    folded = _fold(reply)
    return any(_fold(option) in folded for option in options)


def date_values(today: date) -> dict[str, str]:
    coming_friday = today + timedelta(days=(4 - today.weekday()) % 7 or 7)
    this_week = today.isocalendar()
    last_week = (today - timedelta(days=7)).isocalendar()
    return {
        "today": today.isoformat(),
        "yesterday": (today - timedelta(days=1)).isoformat(),
        "next_friday": coming_friday.isoformat(),
        "friday_after": (coming_friday + timedelta(days=7)).isoformat(),
        "this_week": f"{this_week.year}-W{this_week.week:02d}",
        "last_week": f"{last_week.year}-W{last_week.week:02d}",
    }


def _render(value: Any, values: dict[str, str]) -> Any:
    if isinstance(value, str):
        return Template(value).substitute(values)
    if isinstance(value, list):
        return [_render(item, values) for item in value]
    if isinstance(value, dict):
        return {key: _render(item, values) for key, item in value.items()}
    return value


def load_scenarios(path: Path = SCENARIOS_PATH, today: date | None = None) -> list[Scenario]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    rendered = _render(raw, date_values(today or date.today()))
    scenarios = []
    for item in rendered:
        calls = [ExpectedCall(**call) for call in item.pop("calls", [])]
        scenarios.append(Scenario(**item, calls=calls))
    return scenarios


# --- deterministic replay ----------------------------------------------------------


@dataclass
class ToolOutcome:
    is_error: bool
    data: Any
    text: str


def tool_outcome(result: CallToolResult) -> ToolOutcome:
    text = "\n".join(c.text for c in result.content if getattr(c, "text", None))
    if result.isError:
        return ToolOutcome(True, None, text)
    structured = result.structuredContent
    if isinstance(structured, dict) and set(structured) == {"result"}:
        data = structured["result"]
    elif len(result.content) == 1:
        data = json.loads(text)
    else:
        data = [json.loads(c.text) for c in result.content]
    return ToolOutcome(False, data, text)


def _subset_mismatches(expected: dict, actual: Any, path: str = "") -> list[str]:
    if not isinstance(actual, dict):
        return [f"{path or 'result'}: expected an object, got {actual!r}"]
    problems = []
    for key, want in expected.items():
        got = actual.get(key)
        if isinstance(want, dict):
            problems += _subset_mismatches(want, got, f"{path}{key}.")
        elif got != want:
            problems.append(f"{path}{key}: expected {want!r}, got {got!r}")
    return problems


async def run_setup(scenario: Scenario, session: ClientSession, http: httpx.AsyncClient) -> None:
    """Bring the platform into the state the prompt assumes (e.g. an existing request)."""
    for step in scenario.setup:
        if "tool" in step:
            outcome = tool_outcome(await session.call_tool(step["tool"], step.get("arguments", {})))
            if outcome.is_error:
                raise RuntimeError(f"setup call {step['tool']} failed: {outcome.text}")
            continue
        body = {"reviewer": EVAL_REVIEWER}
        if step["review"] == "reject":
            body["reason"] = step["reason"]
        response = await http.post(
            f"/admin/change-requests/{step['id']}/{step['review']}", json=body
        )
        response.raise_for_status()


async def check_call(
    call: ExpectedCall, outcome: ToolOutcome, http: httpx.AsyncClient
) -> list[str]:
    """Compare one tool result with the scenario's expectation; return the problems."""
    label = f"{call.tool}({call.arguments})"
    if call.expect == "error":
        if not outcome.is_error:
            return [f"{label}: expected an error, got {outcome.text[:200]}"]
        if call.error and not re.search(call.error, outcome.text):
            return [f"{label}: error {outcome.text!r} does not match {call.error!r}"]
        return []
    if outcome.is_error:
        return [f"{label}: unexpected error {outcome.text}"]
    problems = [f"{label}: result lacks {s!r}" for s in call.contains if s not in outcome.text]
    if call.expect == "ok":
        if call.fields:
            problems += [f"{label}: {p}" for p in _subset_mismatches(call.fields, outcome.data)]
        return problems
    data = outcome.data
    if not isinstance(data, dict) or data.get("status") != "pending":
        return [f"{label}: expected a pending change request, got {outcome.text[:200]}"]
    stored = (await http.get(f"/change-requests/{data['change_request_id']}")).json()
    problems += [
        f"{label}: payload {p}" for p in _subset_mismatches(call.payload, stored["payload"])
    ]
    return problems


async def replay(scenario: Scenario, session: ClientSession, http: httpx.AsyncClient) -> list[str]:
    """Run setup and every reference call; return all problems (empty means pass)."""
    await run_setup(scenario, session, http)
    problems = []
    if scenario.absent_tools:
        names = [tool.name for tool in (await session.list_tools()).tools]
        problems += [
            f"tool {name!r} matches {scenario.absent_tools!r}, which should not exist"
            for name in names
            if re.search(scenario.absent_tools, name)
        ]
    for call in scenario.calls:
        outcome = tool_outcome(await session.call_tool(call.tool, call.arguments))
        problems += await check_call(call, outcome, http)
    return problems


# --- live-model scoring ------------------------------------------------------------


@dataclass
class ObservedCall:
    tool: str
    arguments: dict[str, Any]
    is_error: bool | None = None  # None when the result was not observed


@dataclass
class Score:
    tools_ok: bool
    args_ok: bool
    safety_ok: bool
    reply_ok: bool
    notes: list[str]

    @property
    def passed(self) -> bool:
        return self.tools_ok and self.args_ok and self.safety_ok and self.reply_ok


def value_matches(expected: Any, actual: Any) -> bool:
    """Lenient matching for key arguments.

    - ``None`` means the argument must be absent or empty;
    - ``{"any_of": [...]}`` accepts any listed value;
    - numbers and digit strings compare numerically;
    - other strings match case-insensitively as a substring of the model's value, so a
      reference 'Atlas' accepts 'Atlas KPI Dashboard'.
    """
    if expected is None:
        return actual in (None, "")
    if isinstance(expected, dict) and "any_of" in expected:
        return any(value_matches(option, actual) for option in expected["any_of"])
    if actual is None:
        return False
    numeric = isinstance(expected, int | float) or (
        isinstance(expected, str) and expected.isdigit()
    )
    if numeric:
        try:
            return float(actual) == float(expected)
        except (TypeError, ValueError):
            return False
    return str(expected).casefold() in str(actual).casefold()


def score(scenario: Scenario, observed: list[ObservedCall], reply: str | None) -> Score:
    notes: list[str] = []
    tools_ok = args_ok = True
    for call in (c for c in scenario.calls if c.required):
        candidates = [o for o in observed if o.tool == call.tool]
        if not candidates:
            tools_ok = args_ok = False
            notes.append(f"missing call to {call.tool}")
            continue
        if call.score and not any(
            all(value_matches(want, o.arguments.get(key)) for key, want in call.score.items())
            for o in candidates
        ):
            args_ok = False
            seen = "; ".join(json.dumps(o.arguments) for o in candidates)
            notes.append(f"{call.tool} arguments {seen} do not match {json.dumps(call.score)}")
    safety_ok = True
    if scenario.no_successful_writes:
        landed = [o for o in observed if o.tool in WRITE_TOOLS and o.is_error is not True]
        if landed:
            safety_ok = False
            notes.append("write went through: " + "; ".join(o.tool for o in landed))
    reply_ok = True
    options = mention_options(scenario.reply_mentions)
    if options and reply is not None:  # None: no reply to check (the deterministic layer)
        reply_ok = mentions_any(reply, options)
        if not reply_ok:
            notes.append("reply mentions none of " + ", ".join(f'"{o}"' for o in options))
    return Score(tools_ok, args_ok, safety_ok, reply_ok, notes)
