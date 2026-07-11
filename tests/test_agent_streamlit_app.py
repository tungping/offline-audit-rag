import subprocess
from pathlib import Path

from streamlit.testing.v1 import AppTest

from agent_runtime.demo_factory import build_demo_runtime
from agent_runtime.evidence import source_sha256
from agent_runtime.models import SessionStatus, Workspace
from agent_runtime.session_validator import validate_session_bundle


MEETING_TEXT = Path("examples/agent_demo/meeting_with_gaps.txt").read_text(
    encoding="utf-8"
)


def test_demo_factory_uses_real_runtime_and_valid_artifacts(tmp_path: Path):
    runtime, knowledge_version = build_demo_runtime(
        workspace=Workspace.MEETING_AUDIT,
        source_text=MEETING_TEXT,
        source_name="meeting_with_gaps.txt",
        session_root=tmp_path / "sessions",
    )
    session = runtime.create_session(
        goal="检查发布流程和任务完整性",
        source_name="meeting_with_gaps.txt",
        source_sha256=source_sha256(MEETING_TEXT),
        model_name="deterministic-test-adapter",
        knowledge_version=knowledge_version,
    )
    runtime.approve(session.session_id)
    session = runtime.run_until_pause(session.session_id)
    assert session.status is SessionStatus.WAITING_FOR_CLARIFICATION
    runtime.skip_clarification(session.session_id)
    session = runtime.run_until_pause(session.session_id)
    assert session.status is SessionStatus.COMPLETED
    session_dir = runtime.store.artifact_dir(session.session_id).parent
    assert validate_session_bundle(session_dir).valid


def make_app(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AGENT_DEMO_TEST_MODE", "1")
    monkeypatch.setenv("AGENT_DEMO_SESSION_ROOT", str(tmp_path / "sessions"))
    return AppTest.from_file("webui.py", default_timeout=10).run()


def test_app_navigation_and_workspace_controls_are_deterministic(
    monkeypatch, tmp_path: Path
):
    at = make_app(monkeypatch, tmp_path)
    assert not at.exception
    assert list(at.radio(key="experience_selector").options) == [
        "Agent Demo",
        "Classic Audit",
    ]
    assert list(at.radio(key="agent_mode_selector").options) == ["LIVE", "REPLAY"]
    assert list(at.selectbox(key="agent_workspace_selector").options) == [
        "Technical Project Meeting Audit",
        "Synthetic SiC Patent Research",
    ]
    assert at.button(key="agent_approve").label == "Approve Plan & Run"
    assert any("Proposed Plan / Approve" in item.value for item in at.subheader)


def test_app_requires_material_before_approval(monkeypatch, tmp_path: Path):
    at = make_app(monkeypatch, tmp_path)
    at.button(key="agent_approve").click().run()
    assert any("Goal and text material are required" in item.value for item in at.warning)


def test_app_meeting_clarification_replay_and_classic_flow(monkeypatch, tmp_path: Path):
    at = make_app(monkeypatch, tmp_path)
    at.text_area(key="agent_material").set_value(MEETING_TEXT)
    at.button(key="agent_approve").click().run(timeout=10)
    assert not at.exception
    assert any(item.value == "4. Clarification" for item in at.subheader)
    assert sum(
        button.key == "agent_skip_clarification" for button in at.button
    ) == 1

    at.button(key="agent_skip_clarification").click().run(timeout=10)
    assert not at.exception
    session_id = at.session_state["agent_session_id"]
    session_dir = tmp_path / "sessions" / session_id
    assert validate_session_bundle(session_dir).valid
    assert not any(
        button.key == "agent_skip_clarification" for button in at.button
    )

    at.radio(key="agent_mode_selector").set_value("REPLAY").run()
    at.text_input(key="agent_replay_path").set_value(str(session_dir)).run()
    assert not at.error
    assert any("🔵 REPLAY" in item.value for item in at.caption)

    at.radio(key="experience_selector").set_value("Classic Audit").run()
    assert any(item.value == "Classic Audit" for item in at.header)


def test_app_switches_to_patent_workspace_without_ollama(monkeypatch, tmp_path: Path):
    at = make_app(monkeypatch, tmp_path)
    at.selectbox(key="agent_workspace_selector").set_value(
        "Synthetic SiC Patent Research"
    ).run()
    assert at.session_state["agent_workspace"] == Workspace.PATENT_RESEARCH.value
    assert any("extract_features" in item.value for item in at.markdown)


def test_playwright_script_contract_is_self_checking():
    completed = subprocess.run(
        ["bash", "scripts/playwright_agent_smoke.sh", "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "npx: ok" in completed.stdout
    assert "playwright wrapper: ok" in completed.stdout
    assert "streamlit: ok" in completed.stdout
    assert "fake mode: AGENT_DEMO_TEST_MODE=1" in completed.stdout
    assert "output: output/playwright" in completed.stdout
    assert "cleanup trap: configured" in completed.stdout
