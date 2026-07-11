import os
from pathlib import Path

import pytest

from agent_cli import build_live_runtime
from agent_runtime.evidence import source_sha256
from agent_runtime.models import SessionStatus, Workspace
from agent_runtime.session_store import SessionStore
from agent_runtime.session_validator import validate_session_bundle
from audit_core.config import AUDIT_MODEL, EMBED_MODEL
from audit_core.model_io import check_ollama_status


pytestmark = pytest.mark.ollama
RUN_LIVE = os.getenv("RUN_OLLAMA_SMOKE") == "1"


def require_ollama() -> None:
    if not RUN_LIVE:
        pytest.skip("set RUN_OLLAMA_SMOKE=1 to run real local-model smoke tests")
    status = check_ollama_status()
    if not status["connected"]:
        pytest.skip(f"Ollama is unavailable: {status['error']}")
    missing = []
    if not status["audit_model_ok"]:
        missing.append(AUDIT_MODEL)
    if not status["embed_model_ok"]:
        missing.append(EMBED_MODEL)
    if missing:
        pytest.skip("missing local Ollama model(s): " + ", ".join(missing))


def run_live_smoke(tmp_path: Path, workspace: Workspace, source_path: Path):
    require_ollama()
    source_text = source_path.read_text(encoding="utf-8")
    runtime, knowledge_version = build_live_runtime(
        workspace=workspace,
        source_text=source_text,
        source_name=source_path.name,
    )
    runtime.store = SessionStore(tmp_path / "sessions")
    session = runtime.create_session(
        goal=(
            "检查发布流程、任务完整性和敏感信息"
            if workspace is Workspace.MEETING_AUDIT
            else "检索与沟槽底部屏蔽结构相关的 synthetic patents"
        ),
        source_name=source_path.name,
        source_sha256=source_sha256(source_text),
        model_name=AUDIT_MODEL,
        knowledge_version=knowledge_version,
    )
    runtime.approve(session.session_id)
    session = runtime.run_until_pause(session.session_id)
    if session.status is SessionStatus.WAITING_FOR_CLARIFICATION:
        runtime.skip_clarification(session.session_id)
        session = runtime.run_until_pause(session.session_id)
    assert session.status is SessionStatus.COMPLETED, session.error
    assert session.model_calls <= session.budget.max_model_calls
    assert session.tool_calls <= session.budget.max_tool_calls
    assert session.query_rounds <= session.budget.max_query_rounds
    session_dir = runtime.store.artifact_dir(session.session_id).parent
    report = validate_session_bundle(session_dir)
    assert report.valid, report.errors
    return runtime.store.artifact_dir(session.session_id)


def test_real_ollama_meeting_golden_path(tmp_path: Path):
    artifact_dir = run_live_smoke(
        tmp_path,
        Workspace.MEETING_AUDIT,
        Path("examples/agent_demo/meeting_with_gaps.txt"),
    )
    assert (artifact_dir / "meeting_audit_report.md").exists()
    assert (artifact_dir / "tasks.csv").exists()
    assert (artifact_dir / "risk_items.csv").exists()


def test_real_ollama_patent_golden_path(tmp_path: Path):
    artifact_dir = run_live_smoke(
        tmp_path,
        Workspace.PATENT_RESEARCH,
        Path("examples/agent_demo/sic_trench_product_brief.txt"),
    )
    assert (artifact_dir / "patent_research_report.md").exists()
    assert (artifact_dir / "patent_retrieval_results.csv").exists()
    assert (artifact_dir / "claim_chart.csv").exists()
