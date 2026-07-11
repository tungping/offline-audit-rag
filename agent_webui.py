import queue
import threading
import time
from pathlib import Path
from typing import Any, MutableMapping

import streamlit as st

from agent_cli import build_live_runtime
from agent_runtime.evidence import source_sha256
from agent_runtime.models import SessionStatus, Workspace
from agent_runtime.replay import ReplayError, load_replay
from capabilities.meeting_audit.playbook import build_meeting_capability
from capabilities.patent_research.playbook import build_patent_capability


WORKSPACE_LABELS = {
    "Technical Project Meeting Audit": Workspace.MEETING_AUDIT.value,
    "Synthetic SiC Patent Research": Workspace.PATENT_RESEARCH.value,
}
AGENT_STATE_DEFAULTS = {
    "agent_workspace": Workspace.MEETING_AUDIT.value,
    "agent_mode": "LIVE",
    "agent_session_id": "",
    "agent_running": False,
    "agent_error": "",
    "agent_cancel_event": None,
    "agent_worker_thread": None,
    "agent_worker_queue": None,
    "agent_pending_questions": [],
    "agent_runtime": None,
    "agent_store": None,
}


def init_agent_state(state: MutableMapping[str, Any] | None = None) -> None:
    target = st.session_state if state is None else state
    for key, value in AGENT_STATE_DEFAULTS.items():
        if key not in target:
            target[key] = value


def material_policy(workspace: Workspace) -> dict[str, Any]:
    if workspace not in {Workspace.MEETING_AUDIT, Workspace.PATENT_RESEARCH}:
        raise ValueError("unsupported agent workspace")
    return {"paste": True, "extensions": ("txt",), "audio": False}


def agent_mode_badge(mode: str) -> str:
    if mode == "LIVE":
        return "🟢 LIVE"
    if mode == "REPLAY":
        return "🔵 REPLAY"
    raise ValueError("agent mode must be LIVE or REPLAY")


def sanitize_timeline_event(event: Any) -> dict[str, Any]:
    if hasattr(event, "to_dict"):
        event = event.to_dict()
    payload = dict(event.get("payload", {}))
    return {
        "kind": str(event.get("kind", "")),
        "stage": str(event.get("stage", "")),
        "summary": str(payload.get("summary", "")),
        "reason_summary": str(payload.get("reason_summary", payload.get("summary", ""))),
        "tool_name": str(payload.get("tool_name", "")),
        "evidence_ids": list(payload.get("evidence_ids", [])),
    }


def can_submit_clarification(session) -> bool:
    already_submitted = any(
        observation.get("kind") == "clarification"
        for observation in session.observations
    )
    return (
        session.status is SessionStatus.WAITING_FOR_CLARIFICATION
        and session.clarification_rounds <= 1
        and not already_submitted
    )


def cancel_agent_session(cancel_event, runtime, session_id: str) -> None:
    cancel_event.set()
    runtime.cancel(session_id)


def run_agent_worker(runtime, session_id: str, cancel_event, result_queue) -> None:
    try:
        session = runtime.run_until_pause(
            session_id, cancel_checker=cancel_event.is_set
        )
        result_queue.put((session.session_id, session.status.value))
    except Exception:
        result_queue.put((session_id, SessionStatus.FAILED.value))


def _capability(workspace: Workspace):
    return (
        build_meeting_capability()
        if workspace is Workspace.MEETING_AUDIT
        else build_patent_capability()
    )


def _start_worker(runtime, session_id: str) -> None:
    cancel_event = threading.Event()
    result_queue = queue.Queue()
    thread = threading.Thread(
        target=run_agent_worker,
        args=(runtime, session_id, cancel_event, result_queue),
        daemon=True,
    )
    st.session_state.agent_running = True
    st.session_state.agent_cancel_event = cancel_event
    st.session_state.agent_worker_queue = result_queue
    st.session_state.agent_worker_thread = thread
    thread.start()


def _poll_worker() -> None:
    result_queue = st.session_state.get("agent_worker_queue")
    if result_queue is None:
        return
    try:
        session_id, status = result_queue.get_nowait()
    except queue.Empty:
        return
    st.session_state.agent_session_id = session_id
    st.session_state.agent_running = False
    if status == SessionStatus.FAILED.value:
        st.session_state.agent_error = "Agent worker failed; inspect the durable session events."


def _render_plan(workspace: Workspace) -> None:
    st.subheader("2. Proposed Plan / Approve")
    capability = _capability(workspace)
    for index, stage in enumerate(capability.stages, 1):
        optional = " — optional" if stage not in capability.required_stages else ""
        st.write(f"{index}. `{stage}`{optional}")
    st.caption(
        f"Budget: {capability.budget.max_model_calls} model calls, "
        f"{capability.budget.max_tool_calls} tool calls, "
        f"{capability.budget.max_query_rounds} query rounds."
    )


def _render_timeline_and_artifacts(session_dir: Path) -> None:
    try:
        replay = load_replay(session_dir)
    except ReplayError as exc:
        st.warning(str(exc))
        return
    st.subheader("3. Execution Timeline")
    for event in replay.events:
        visible = sanitize_timeline_event(event)
        st.write(
            f"`{visible['stage'] or '-'}` · {visible['kind']} · "
            f"{visible['reason_summary']}"
        )
    st.subheader("5. Evidence & Artifacts")
    st.caption(f"Verified evidence records: {len(replay.evidence)}")
    for path in replay.artifact_manifest:
        st.write(f"- `{path}`")


def _render_replay() -> None:
    session_path = st.text_input("Session directory", placeholder="sessions/<session-id>")
    if not session_path:
        st.info("Replay is read-only and performs no model, embedding, planner, or tool calls.")
        return
    try:
        replay = load_replay(Path(session_path))
    except ReplayError as exc:
        st.error(str(exc))
        return
    st.success(
        f"{agent_mode_badge('REPLAY')} · {replay.metadata['workspace']} · "
        f"{replay.metadata['status']}"
    )
    st.write(f"Model: `{replay.metadata['model_name']}`")
    st.write(f"Knowledge version: `{replay.metadata['knowledge_version']}`")
    _render_timeline_and_artifacts(Path(session_path))


def render_agent_demo() -> None:
    init_agent_state()
    _poll_worker()
    st.header("Agent Demo")
    mode = st.radio("Mode", ["LIVE", "REPLAY"], horizontal=True)
    st.session_state.agent_mode = mode
    st.caption(agent_mode_badge(mode))
    if mode == "REPLAY":
        _render_replay()
        return

    label = st.selectbox("Workspace", list(WORKSPACE_LABELS))
    workspace = Workspace(WORKSPACE_LABELS[label])
    st.session_state.agent_workspace = workspace.value
    st.subheader("1. Goal & Material")
    default_goal = (
        "检查会议中的发布流程和任务完整性"
        if workspace is Workspace.MEETING_AUDIT
        else "检索与沟槽底部屏蔽结构相关的 synthetic patents"
    )
    goal = st.text_input("Goal", value=default_goal)
    uploaded = st.file_uploader("Upload TXT", type=list(material_policy(workspace)["extensions"]), key="agent_upload")
    pasted = st.text_area("Paste text material", height=180, key="agent_material")
    source_text = pasted
    source_name = "agent_webui_input.txt"
    if uploaded is not None:
        try:
            source_text = uploaded.getvalue().decode("utf-8-sig")
            source_name = uploaded.name
        except UnicodeDecodeError:
            st.error("Agent Demo currently accepts UTF-8 TXT files only.")
            source_text = ""

    _render_plan(workspace)
    if st.button("Approve Plan & Run", type="primary", disabled=st.session_state.agent_running):
        if not goal.strip() or not source_text.strip():
            st.warning("Goal and text material are required.")
        else:
            try:
                runtime, knowledge_version = build_live_runtime(
                    workspace=workspace,
                    source_text=source_text,
                    source_name=source_name,
                )
                session = runtime.create_session(
                    goal=goal,
                    source_name=source_name,
                    source_sha256=source_sha256(source_text),
                    model_name="local-ollama",
                    knowledge_version=knowledge_version,
                )
                runtime.approve(session.session_id)
                st.session_state.agent_runtime = runtime
                st.session_state.agent_store = runtime.store
                st.session_state.agent_session_id = session.session_id
                st.session_state.agent_error = ""
                _start_worker(runtime, session.session_id)
                st.rerun()
            except Exception as exc:
                st.session_state.agent_error = str(exc)

    if st.session_state.agent_running:
        st.info("Agent is running serially. Progress is persisted to the session bundle.")
        if st.button("Cancel Agent"):
            cancel_agent_session(
                st.session_state.agent_cancel_event,
                st.session_state.agent_runtime,
                st.session_state.agent_session_id,
            )
            st.rerun()
        time.sleep(0.5)
        _poll_worker()
        st.rerun()

    session_id = st.session_state.agent_session_id
    store = st.session_state.agent_store
    runtime = st.session_state.agent_runtime
    if session_id and store is not None:
        session = store.load(session_id)
        if can_submit_clarification(session):
            st.subheader("4. Clarification")
            st.write(session.pending_question)
            answer = st.text_area("Clarification answer", key="agent_clarification_answer")
            cols = st.columns(2)
            if cols[0].button("Submit clarification") and answer.strip():
                runtime.submit_clarification(session_id, answer)
                _start_worker(runtime, session_id)
                st.rerun()
            if cols[1].button("Skip clarification"):
                runtime.skip_clarification(session_id)
                _start_worker(runtime, session_id)
                st.rerun()
        _render_timeline_and_artifacts(store.artifact_dir(session_id).parent)
    if st.session_state.agent_error:
        st.error(st.session_state.agent_error)
