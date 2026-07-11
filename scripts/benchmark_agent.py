import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_cli import build_live_runtime
from agent_runtime.evidence import source_sha256
from agent_runtime.models import SessionStatus, Workspace
from audit_core.config import AUDIT_MODEL, EMBED_MODEL, OUTPUT
from audit_core.model_io import check_ollama_status


HARDWARE_NOTE = "Apple M1 Pro, 8 CPU cores, 14 GPU cores, 16 GB RAM"


class BenchmarkError(RuntimeError):
    """Raised when a benchmark cannot truthfully start or finish."""


def run_benchmark(
    *,
    workspace: Workspace,
    goal: str,
    input_path: Path,
    output_dir: Path = Path(OUTPUT),
    status_checker=check_ollama_status,
    runtime_factory=build_live_runtime,
    clock=time.perf_counter,
):
    status = status_checker()
    if not status.get("connected"):
        raise BenchmarkError("Ollama is unavailable; start it manually and retry.")
    missing = []
    if not status.get("audit_model_ok"):
        missing.append(AUDIT_MODEL)
    if not status.get("embed_model_ok"):
        missing.append(EMBED_MODEL)
    if missing:
        raise BenchmarkError(
            "Ollama is missing required model(s): " + ", ".join(missing)
        )
    try:
        source_text = input_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BenchmarkError(f"cannot read benchmark input: {exc}") from exc
    if not source_text.strip():
        raise BenchmarkError("benchmark input is empty")

    started = clock()
    try:
        runtime, knowledge_version = runtime_factory(
            workspace=workspace,
            source_text=source_text,
            source_name=input_path.name,
        )
        session = runtime.create_session(
            goal=goal,
            source_name=input_path.name,
            source_sha256=source_sha256(source_text),
            model_name=AUDIT_MODEL,
            knowledge_version=knowledge_version,
        )
        runtime.approve(session.session_id)
        session = runtime.run_until_pause(session.session_id)
        if session.status is SessionStatus.WAITING_FOR_CLARIFICATION:
            runtime.skip_clarification(session.session_id)
            session = runtime.run_until_pause(session.session_id)
    except Exception as exc:
        raise BenchmarkError(f"live session failed to run: {exc}") from exc
    elapsed = round(clock() - started, 3)
    result = {
        "hardware_note": HARDWARE_NOTE,
        "workspace": workspace.value,
        "model": AUDIT_MODEL,
        "embedding_model": EMBED_MODEL,
        "elapsed_seconds": elapsed,
        "model_calls": session.model_calls,
        "tool_calls": session.tool_calls,
        "query_rounds": session.query_rounds,
        "status": session.status.value,
        "session_id": session.session_id,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = output_dir / f"agent_benchmark_{workspace.value}_{stamp}.json"
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_path, result


def build_parser():
    parser = argparse.ArgumentParser(
        description="Measure one serial local-agent session; this is not an SLA."
    )
    parser.add_argument(
        "--workspace",
        choices=[item.value for item in Workspace],
        required=True,
    )
    parser.add_argument("--goal", required=True)
    parser.add_argument("--input", type=Path, required=True)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output_path, result = run_benchmark(
            workspace=Workspace(args.workspace),
            goal=args.goal,
            input_path=args.input,
        )
    except BenchmarkError as exc:
        print(f"Benchmark error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"Saved: {output_path}")
    return 0 if result["status"] == SessionStatus.COMPLETED.value else 3


if __name__ == "__main__":
    sys.exit(main())
