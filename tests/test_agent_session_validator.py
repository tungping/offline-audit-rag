import json
import subprocess
import sys
from pathlib import Path

from agent_runtime.evidence import source_sha256, verify_quote
from agent_runtime.models import AgentSession, ResourceBudget, SessionStatus, Workspace
from agent_runtime.session_store import SessionStore
from agent_runtime.session_validator import validate_session_bundle


def make_bundle(tmp_path: Path, workspace=Workspace.MEETING_AUDIT):
    store = SessionStore(tmp_path / "sessions")
    session = AgentSession.new(
        workspace=workspace,
        goal="validate bundle",
        source_name="input.txt",
        source_sha256=source_sha256("source"),
        model_name="fake",
        knowledge_version="v1",
        budget=(
            ResourceBudget.meeting_default()
            if workspace is Workspace.MEETING_AUDIT
            else ResourceBudget.patent_default()
        ),
    )
    session.status = SessionStatus.COMPLETED
    session_dir = store.create(session)
    evidence = verify_quote(
        source_type="meeting" if workspace is Workspace.MEETING_AUDIT else "synthetic_patent",
        source_id="input.txt",
        locator="line:1",
        source_text="verified source quote",
        quote="verified source quote",
    )
    store.save_evidence(session.session_id, [evidence])
    artifact_dir = store.artifact_dir(session.session_id)
    if workspace is Workspace.MEETING_AUDIT:
        names = ("meeting_audit_report.md", "tasks.csv", "risk_items.csv")
    else:
        names = (
            "patent_research_report.md",
            "patent_retrieval_results.csv",
            "claim_chart.csv",
        )
    for name in names:
        (artifact_dir / name).write_text(
            f"evidence_id,{evidence.evidence_id}\n", encoding="utf-8"
        )
    session.artifact_paths = [str(artifact_dir / name) for name in names]
    store.save(session)
    return store, session, session_dir, evidence.evidence_id


def test_valid_completed_bundle_passes(tmp_path: Path):
    _, session, session_dir, _ = make_bundle(tmp_path)
    report = validate_session_bundle(session_dir)
    assert report.valid is True
    assert report.session_id == session.session_id
    assert report.errors == ()
    assert len(report.checked_artifacts) == 3


def test_validator_reports_malformed_session_json(tmp_path: Path):
    _, _, session_dir, _ = make_bundle(tmp_path)
    (session_dir / "session.json").write_text("{bad", encoding="utf-8")
    report = validate_session_bundle(session_dir)
    assert report.valid is False
    assert any(issue.code == "malformed_bundle" for issue in report.errors)


def test_validator_rejects_dangling_evidence_reference(tmp_path: Path):
    _, _, session_dir, _ = make_bundle(tmp_path)
    report_path = session_dir / "artifacts" / "meeting_audit_report.md"
    report_path.write_text("evidence: " + "f" * 32, encoding="utf-8")
    report = validate_session_bundle(session_dir)
    assert any(issue.code == "dangling_evidence" for issue in report.errors)


def test_validator_rejects_budget_overrun_and_escaped_artifact(tmp_path: Path):
    store, session, session_dir, _ = make_bundle(tmp_path)
    session.model_calls = session.budget.max_model_calls + 1
    session.artifact_paths.append(str(tmp_path / "outside.md"))
    store.save(session)
    report = validate_session_bundle(session_dir)
    codes = {issue.code for issue in report.errors}
    assert {"budget_exceeded", "artifact_escape"} <= codes


def test_validator_enforces_terminal_artifact_contracts(tmp_path: Path):
    _, _, meeting_dir, _ = make_bundle(tmp_path / "meeting")
    (meeting_dir / "artifacts" / "tasks.csv").unlink()
    meeting_report = validate_session_bundle(meeting_dir)
    assert any(issue.code == "missing_artifact" for issue in meeting_report.errors)

    store, session, patent_dir, _ = make_bundle(
        tmp_path / "patent", Workspace.PATENT_RESEARCH
    )
    session.status = SessionStatus.INCOMPLETE
    store.save(session)
    patent_report = validate_session_bundle(patent_dir)
    assert any(issue.code == "false_completion" for issue in patent_report.errors)


def test_validator_rejects_unmasked_meeting_sensitive_data(tmp_path: Path):
    _, _, session_dir, _ = make_bundle(tmp_path)
    (session_dir / "artifacts" / "tasks.csv").write_text(
        "phone\n13812345678\n", encoding="utf-8"
    )
    report = validate_session_bundle(session_dir)
    assert any(issue.code == "sensitive_data" for issue in report.errors)


def test_validator_cli_json_and_exit_codes(tmp_path: Path):
    _, session, session_dir, _ = make_bundle(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/validate_agent_session.py",
            str(session_dir),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["session_id"] == session.session_id

    (session_dir / "session.json").write_text("{}", encoding="utf-8")
    failed = subprocess.run(
        [sys.executable, "scripts/validate_agent_session.py", str(session_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode == 2
