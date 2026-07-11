import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_runtime.models import SessionStatus, Workspace
from scripts.benchmark_agent import BenchmarkError, run_benchmark


class FakeRuntime:
    def __init__(self, final_status=SessionStatus.COMPLETED):
        self.final_status = final_status
        self.skipped = []
        self.created = None

    def create_session(self, **kwargs):
        self.created = kwargs
        return SimpleNamespace(session_id="a" * 32)

    def approve(self, session_id):
        return None

    def run_until_pause(self, session_id):
        if not self.skipped:
            return SimpleNamespace(
                session_id=session_id,
                status=SessionStatus.WAITING_FOR_CLARIFICATION,
                model_calls=1,
                tool_calls=3,
                query_rounds=0,
            )
        return SimpleNamespace(
            session_id=session_id,
            status=self.final_status,
            model_calls=2,
            tool_calls=6,
            query_rounds=1,
        )

    def skip_clarification(self, session_id):
        self.skipped.append(session_id)


def test_benchmark_fails_clearly_without_ollama_and_writes_nothing(tmp_path: Path):
    source = tmp_path / "input.txt"
    source.write_text("meeting", encoding="utf-8")
    built = []

    with pytest.raises(BenchmarkError, match="Ollama"):
        run_benchmark(
            workspace=Workspace.MEETING_AUDIT,
            goal="audit",
            input_path=source,
            output_dir=tmp_path / "output",
            status_checker=lambda: {
                "connected": False,
                "audit_model_ok": False,
                "embed_model_ok": False,
            },
            runtime_factory=lambda **kwargs: built.append(kwargs),
        )

    assert not built
    output_dir = tmp_path / "output"
    assert not output_dir.exists() or not list(output_dir.glob("*.json"))


def test_benchmark_reports_live_start_failure_without_writing_result(tmp_path: Path):
    source = tmp_path / "input.txt"
    source.write_text("meeting", encoding="utf-8")
    output_dir = tmp_path / "output"

    with pytest.raises(BenchmarkError, match="live session failed"):
        run_benchmark(
            workspace=Workspace.MEETING_AUDIT,
            goal="audit",
            input_path=source,
            output_dir=output_dir,
            status_checker=lambda: {
                "connected": True,
                "audit_model_ok": True,
                "embed_model_ok": True,
            },
            runtime_factory=lambda **kwargs: (_ for _ in ()).throw(
                RuntimeError("model crashed")
            ),
        )

    assert not output_dir.exists()


def test_benchmark_records_measured_session_facts(tmp_path: Path):
    source = tmp_path / "input.txt"
    source.write_text("meeting material", encoding="utf-8")
    runtime = FakeRuntime()
    ticks = iter([100.0, 112.5])

    output_path, result = run_benchmark(
        workspace=Workspace.MEETING_AUDIT,
        goal="audit",
        input_path=source,
        output_dir=tmp_path / "output",
        status_checker=lambda: {
            "connected": True,
            "audit_model_ok": True,
            "embed_model_ok": True,
        },
        runtime_factory=lambda **kwargs: (runtime, "rules-v1"),
        clock=lambda: next(ticks),
    )

    persisted = json.loads(output_path.read_text(encoding="utf-8"))
    assert persisted == result
    assert result["elapsed_seconds"] == 12.5
    assert result["model_calls"] == 2
    assert result["tool_calls"] == 6
    assert result["query_rounds"] == 1
    assert result["status"] == "COMPLETED"
    assert result["session_id"] == "a" * 32
    assert runtime.skipped == ["a" * 32]


def test_benchmark_script_runs_directly_from_repository_root():
    completed = subprocess.run(
        [sys.executable, "scripts/benchmark_agent.py", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--workspace" in completed.stdout
