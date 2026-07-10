import json
from pathlib import Path

import pytest

from agent_runtime.evidence import source_sha256, verify_quote
from agent_runtime.models import (
    AgentEvent,
    AgentSession,
    ResourceBudget,
    SessionStatus,
    Workspace,
)
from agent_runtime.session_store import SessionStore


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
