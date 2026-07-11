import json
from pathlib import Path

import pytest

from agent_cli import main as cli_main
from agent_runtime.evidence import source_sha256
from agent_runtime.models import AgentEvent, AgentSession, ResourceBudget, Workspace
from agent_runtime.replay import ReplayError, iter_replay_events, load_replay
from agent_runtime.session_store import SessionStore


def make_replay_bundle(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    session = AgentSession.new(
        workspace=Workspace.MEETING_AUDIT,
        goal="replay this session",
        source_name="meeting.txt",
        source_sha256=source_sha256("meeting"),
        model_name="demo-model",
        knowledge_version="rules-v1",
        budget=ResourceBudget.meeting_default(),
    )
    artifact = str(tmp_path / "historical" / "report.md")
    session.artifact_paths = [artifact]
    session_dir = store.create(session)
    store.append_event(session.session_id, AgentEvent.status("first", "parse"))
    store.append_event(session.session_id, AgentEvent.status("second", "verify"))
    return session_dir, artifact


def test_replay_is_pure_ordered_reader_with_historical_artifacts(tmp_path: Path):
    session_dir, artifact = make_replay_bundle(tmp_path)

    replay = load_replay(session_dir, current_knowledge_version="rules-v2")

    assert replay.mode == "REPLAY"
    assert [event.payload["summary"] for event in replay.events] == ["first", "second"]
    assert replay.artifact_manifest == (artifact,)
    assert replay.knowledge_version_matches is False
    assert tuple(iter_replay_events(replay)) == replay.events
    assert not hasattr(replay, "run")


def test_replay_never_needs_runtime_services(tmp_path: Path):
    session_dir, _ = make_replay_bundle(tmp_path)
    called = {"planner": 0, "model": 0, "embedding": 0, "tool": 0}

    replay = load_replay(session_dir)

    assert replay.metadata["model_name"] == "demo-model"
    assert called == {"planner": 0, "model": 0, "embedding": 0, "tool": 0}


def test_replay_rejects_path_traversal_and_malformed_jsonl(tmp_path: Path):
    session_dir, _ = make_replay_bundle(tmp_path)
    with pytest.raises(ReplayError, match="traversal"):
        load_replay(session_dir.parent / ".." / "sessions" / session_dir.name)

    (session_dir / "events.jsonl").write_text("{bad json}\n", encoding="utf-8")
    with pytest.raises(ReplayError, match="events.jsonl"):
        load_replay(session_dir)


def test_cli_replay_prints_badge_and_metadata(tmp_path: Path):
    session_dir, _ = make_replay_bundle(tmp_path)
    output = []

    exit_code = cli_main(
        ["replay", "--session", str(session_dir)],
        output_func=output.append,
    )

    assert exit_code == 0
    rendered = "\n".join(output)
    assert "[REPLAY]" in rendered
    assert "demo-model" in rendered
    assert "rules-v1" in rendered


def test_cli_live_requires_explicit_approval(tmp_path: Path):
    input_path = tmp_path / "meeting.txt"
    input_path.write_text("会议内容", encoding="utf-8")
    called = []

    class NeverBuildRuntime:
        pass

    exit_code = cli_main(
        [
            "live", "--workspace", "meeting_audit", "--goal", "检查会议",
            "--input", str(input_path),
        ],
        input_func=lambda prompt: "n",
        output_func=called.append,
        runtime_factory=lambda **kwargs: NeverBuildRuntime(),
    )

    assert exit_code == 2
    assert any("Proposed Plan" in line for line in called)
