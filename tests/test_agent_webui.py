import queue
import threading
from types import SimpleNamespace

from agent_runtime.models import SessionStatus, Workspace
from agent_webui import (
    AGENT_STATE_DEFAULTS,
    WORKSPACE_LABELS,
    agent_mode_badge,
    cancel_agent_session,
    can_submit_clarification,
    init_agent_state,
    material_policy,
    run_agent_worker,
    sanitize_timeline_event,
)


def test_workspace_labels_map_to_exact_workspace_ids():
    assert list(WORKSPACE_LABELS.values()) == [
        Workspace.MEETING_AUDIT.value,
        Workspace.PATENT_RESEARCH.value,
    ]


def test_agent_material_policy_is_text_only_and_workspace_scoped():
    meeting = material_policy(Workspace.MEETING_AUDIT)
    patent = material_policy(Workspace.PATENT_RESEARCH)
    assert meeting == {"paste": True, "extensions": ("txt",), "audio": False}
    assert patent == {"paste": True, "extensions": ("txt",), "audio": False}


def test_timeline_hides_prompts_and_keeps_reason_summary():
    visible = sanitize_timeline_event({
        "kind": "tool_result",
        "stage": "parse",
        "payload": {
            "summary": "提取会议结构",
            "reason_summary": "执行当前阶段工具",
            "planner_prompt": "hidden chain",
            "system_prompt": "hidden system",
            "tool_name": "meeting.extract_structure",
        },
    })
    assert visible["reason_summary"] == "执行当前阶段工具"
    assert "planner_prompt" not in visible
    assert "system_prompt" not in visible
    assert "hidden" not in str(visible)


def test_live_and_replay_badges_are_distinct():
    assert agent_mode_badge("LIVE") == "🟢 LIVE"
    assert agent_mode_badge("REPLAY") == "🔵 REPLAY"


def test_clarification_submission_is_allowed_once():
    paused = SimpleNamespace(
        status=SessionStatus.WAITING_FOR_CLARIFICATION,
        clarification_rounds=1,
        observations=[],
    )
    assert can_submit_clarification(paused)
    paused.observations.append({"kind": "clarification", "response": "answer"})
    assert not can_submit_clarification(paused)


def test_cancel_event_reaches_runtime_cancel():
    event = threading.Event()
    cancelled = []
    runtime = SimpleNamespace(cancel=lambda session_id: cancelled.append(session_id))
    cancel_agent_session(event, runtime, "abc")
    assert event.is_set()
    assert cancelled == ["abc"]


def test_agent_state_uses_required_prefix_and_worker_returns_only_id_status():
    state = {}
    init_agent_state(state)
    assert set(AGENT_STATE_DEFAULTS) <= set(state)
    assert all(key.startswith("agent_") for key in AGENT_STATE_DEFAULTS)
    result_queue = queue.Queue()
    runtime = SimpleNamespace(
        run_until_pause=lambda session_id, cancel_checker: SimpleNamespace(
            session_id=session_id, status=SessionStatus.COMPLETED
        )
    )
    run_agent_worker(runtime, "session-id", threading.Event(), result_queue)
    assert result_queue.get_nowait() == ("session-id", "COMPLETED")
