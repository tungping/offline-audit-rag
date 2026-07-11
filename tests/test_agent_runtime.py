import json
from pathlib import Path

import pytest

from agent_runtime.evidence import source_sha256, verify_quote
from agent_runtime.models import (
    AgentAction,
    AgentEvent,
    AgentSession,
    Evidence,
    ResourceBudget,
    SessionStatus,
    Workspace,
)
from agent_runtime.session_store import SessionStore
from agent_runtime.planner import (
    CapabilityDefinition,
    OllamaActionPlanner,
    PlannedAction,
    PlannerError,
    parse_agent_action,
)
from agent_runtime.runtime import AgentRuntime
from agent_runtime.tools import ToolExecutionError, ToolRegistry, ToolResult, ToolSpec


def make_session() -> AgentSession:
    return AgentSession.new(
        workspace=Workspace.MEETING_AUDIT,
        goal="检查发布流程",
        source_name="meeting.txt",
        source_sha256=source_sha256("手机号 13812345678"),
        model_name="qwen3.5:9b",
        knowledge_version="rules-v1",
        budget=ResourceBudget.meeting_default(),
    )


def test_session_store_creates_required_bundle_without_raw_source(tmp_path: Path):
    store = SessionStore(tmp_path)
    session = make_session()
    store.create(session)
    store.append_event(
        session.session_id,
        AgentEvent.status("已读取手机号 13812345678"),
    )

    root = tmp_path / session.session_id
    assert {path.name for path in root.iterdir()} == {
        "request.json",
        "session.json",
        "events.jsonl",
        "evidence.json",
        "artifacts",
    }
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            root / "request.json",
            root / "session.json",
            root / "events.jsonl",
        )
    )
    assert "13812345678" not in persisted
    assert "138****5678" in persisted
    assert json.loads((root / "evidence.json").read_text(encoding="utf-8")) == []


def test_session_round_trip_preserves_nested_types(tmp_path: Path):
    store = SessionStore(tmp_path)
    session = make_session()
    store.create(session)

    loaded = store.load(session.session_id)

    assert loaded == session
    assert loaded.workspace is Workspace.MEETING_AUDIT
    assert loaded.status is SessionStatus.DRAFT_PLAN
    assert loaded.budget == ResourceBudget.meeting_default()


def test_session_store_preserves_evidence_ids_that_contain_phone_like_digits(
    tmp_path: Path,
):
    store = SessionStore(tmp_path)
    session = make_session()
    store.create(session)
    evidence_id = "0b4ffff9b0da416b9a77c15553920893"
    evidence = Evidence(
        evidence_id=evidence_id,
        source_type="synthetic_patent",
        source_id="SYN-SIC-006",
        locator="abstract",
        quote="一种降低界面缺陷并提高栅氧可靠性的处理结构。",
        source_sha256="a" * 64,
    )

    store.save_evidence(session.session_id, [evidence])

    assert store.load_evidence(session.session_id)[0].evidence_id == evidence_id


def test_session_store_rejects_path_traversal(tmp_path: Path):
    store = SessionStore(tmp_path)

    with pytest.raises(ValueError, match="invalid session id"):
        store.load("../.env")


def test_verify_quote_requires_exact_source_text():
    evidence = verify_quote(
        source_type="meeting",
        source_id="meeting.txt",
        locator="line:2",
        source_text="第一行\n直接 push 到 main，没有评审。",
        quote="直接 push 到 main",
    )
    assert evidence.quote == "直接 push 到 main"
    assert evidence.locator == "line:2"

    with pytest.raises(ValueError, match="quote not found"):
        verify_quote(
            source_type="meeting",
            source_id="meeting.txt",
            locator="line:2",
            source_text="原文没有该句",
            quote="模型编造的证据",
        )


def test_verify_quote_rejects_empty_quote():
    with pytest.raises(ValueError, match="quote must not be empty"):
        verify_quote(
            source_type="meeting",
            source_id="meeting.txt",
            locator="line:1",
            source_text="原文",
            quote="",
        )


class FakePlanner:
    def __init__(self, actions):
        self.actions = list(actions)

    def next_action(self, session, capability):
        if not self.actions:
            raise AssertionError("planner action queue exhausted")
        return PlannedAction(action=self.actions.pop(0), model_calls=1)


def make_capability(budget: ResourceBudget | None = None) -> CapabilityDefinition:
    return CapabilityDefinition(
        workspace=Workspace.MEETING_AUDIT,
        display_name="Meeting test",
        stages=("work", "verify", "artifacts"),
        stage_tools={
            "work": frozenset({"meeting.noop"}),
            "verify": frozenset(),
            "artifacts": frozenset(),
        },
        required_stages=frozenset({"work", "verify", "artifacts"}),
        budget=budget or ResourceBudget.meeting_default(),
        planner_system_prompt="Use only allowed tools.",
    )


def make_runtime(
    tmp_path: Path,
    actions,
    budget: ResourceBudget | None = None,
    handler=None,
):
    store = SessionStore(tmp_path / "sessions")
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="meeting.noop",
            workspace=Workspace.MEETING_AUDIT,
            required_args=frozenset(),
            optional_args=frozenset(),
            handler=handler
            or (lambda args, context: ToolResult(summary="ok", data={})),
        )
    )
    runtime = AgentRuntime(
        store=store,
        registry=registry,
        planner=FakePlanner(actions),
        capability=make_capability(budget),
    )
    session = runtime.create_session(
        goal="audit",
        source_name="meeting.txt",
        source_sha256="c" * 64,
        model_name="demo",
        knowledge_version="v1",
    )
    return runtime, session


def test_runtime_completes_only_after_required_stages(tmp_path: Path):
    runtime, session = make_runtime(
        tmp_path,
        [
            AgentAction.advance("verify"),
            AgentAction.advance("artifacts"),
            AgentAction.complete(),
        ],
    )
    runtime.approve(session.session_id)

    result = runtime.run_until_pause(session.session_id)

    assert result.status is SessionStatus.COMPLETED
    assert set(result.completed_stages) == {"work", "verify", "artifacts"}
    assert result.model_calls == 3


def test_runtime_rejects_completion_before_required_stages(tmp_path: Path):
    runtime, session = make_runtime(tmp_path, [AgentAction.complete()])
    runtime.approve(session.session_id)

    result = runtime.run_until_pause(session.session_id)

    assert result.status is SessionStatus.FAILED
    assert "required stages" in result.error


def test_runtime_stops_at_tool_budget(tmp_path: Path):
    budget = ResourceBudget(max_model_calls=4, max_tool_calls=1)
    runtime, session = make_runtime(
        tmp_path,
        [AgentAction.tool("meeting.noop", {}), AgentAction.tool("meeting.noop", {})],
        budget,
    )
    runtime.approve(session.session_id)

    result = runtime.run_until_pause(session.session_id)

    assert result.status is SessionStatus.INCOMPLETE
    assert result.tool_calls == 1


def test_runtime_stops_at_model_budget(tmp_path: Path):
    budget = ResourceBudget(max_model_calls=1, max_tool_calls=2)
    runtime, session = make_runtime(
        tmp_path,
        [AgentAction.advance("verify"), AgentAction.advance("artifacts")],
        budget,
    )
    runtime.approve(session.session_id)

    result = runtime.run_until_pause(session.session_id)

    assert result.status is SessionStatus.INCOMPLETE
    assert result.model_calls == 1


def test_runtime_allows_one_clarification_then_stops(tmp_path: Path):
    runtime, session = make_runtime(
        tmp_path,
        [
            AgentAction.clarification("Who owns this task?"),
            AgentAction.clarification("When is it due?"),
        ],
    )
    runtime.approve(session.session_id)
    paused = runtime.run_until_pause(session.session_id)
    assert paused.status is SessionStatus.WAITING_FOR_CLARIFICATION

    runtime.submit_clarification(session.session_id, "Alice owns it")
    result = runtime.run_until_pause(session.session_id)

    assert result.status is SessionStatus.INCOMPLETE
    assert result.clarification_rounds == 1


def test_runtime_cancel_before_tool_keeps_zero_tool_calls(tmp_path: Path):
    runtime, session = make_runtime(
        tmp_path, [AgentAction.tool("meeting.noop", {})]
    )
    runtime.approve(session.session_id)

    result = runtime.cancel(session.session_id)

    assert result.status is SessionStatus.CANCELLED
    assert result.tool_calls == 0


def test_runtime_retries_only_retryable_tool_failure_once(tmp_path: Path):
    attempts = {"count": 0}

    def flaky_handler(args, context):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ToolExecutionError("temporary failure", retryable=True)
        return ToolResult(summary="ok", data={})

    runtime, session = make_runtime(
        tmp_path,
        [
            AgentAction.tool("meeting.noop", {}),
            AgentAction.advance("verify"),
            AgentAction.advance("artifacts"),
            AgentAction.complete(),
        ],
        handler=flaky_handler,
    )
    runtime.approve(session.session_id)

    result = runtime.run_until_pause(session.session_id)

    assert result.status is SessionStatus.COMPLETED
    assert attempts["count"] == 2
    assert result.tool_calls == 2


def test_parse_agent_action_rejects_unknown_keys():
    with pytest.raises(ValueError, match="unknown action keys"):
        parse_agent_action(
            '{"kind":"COMPLETE","reason_summary":"done","shell":"rm -rf /"}'
        )


def test_ollama_planner_repairs_invalid_json_once():
    outputs = iter(
        [
            "not json",
            '{"kind":"COMPLETE","reason_summary":"done","tool_name":"",'
            '"arguments":{},"next_stage":"","question":""}',
        ]
    )
    planner = OllamaActionPlanner(generate=lambda system, prompt: next(outputs))

    decision = planner.next_action(make_session(), make_capability())

    assert decision.action.kind.value == "COMPLETE"
    assert decision.model_calls == 2


def test_ollama_planner_fails_after_one_repair():
    planner = OllamaActionPlanner(generate=lambda system, prompt: "still invalid")

    with pytest.raises(PlannerError) as error:
        planner.next_action(make_session(), make_capability())

    assert error.value.model_calls == 2
