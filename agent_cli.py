import argparse
import hashlib
import sys
import uuid
from pathlib import Path
from typing import Callable

import chromadb
import ollama

from agent_runtime.evidence import source_sha256
from agent_runtime.models import SessionStatus, Workspace
from agent_runtime.replay import ReplayError, load_replay
from agent_runtime.runtime import AgentRuntime
from agent_runtime.session_store import SessionStore
from agent_runtime.tools import ToolRegistry
from audit_core.config import AUDIT_MODEL, CONFIG_DIR, EMBED_MODEL, SESSIONS_DIR
from audit_core.knowledge_base import initialize_knowledge_base, retrieve_relevant_context
from audit_core.model_io import generate_json_stream
from capabilities.meeting_audit.playbook import MeetingPlaybookPlanner, build_meeting_capability
from capabilities.meeting_audit.tools import register_meeting_tools
from capabilities.patent_research.corpus import corpus_version, load_corpus
from capabilities.patent_research.playbook import PatentPlaybookPlanner, build_patent_capability
from capabilities.patent_research.search import build_semantic_index, semantic_search
from capabilities.patent_research.tools import register_patent_tools


CORPUS_PATH = Path("capabilities/patent_research/corpus/synthetic_sic_patents.jsonl")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bounded local agent demo")
    commands = parser.add_subparsers(dest="command", required=True)
    live = commands.add_parser("live", help="run a new local agent session")
    live.add_argument("--workspace", choices=[item.value for item in Workspace], required=True)
    live.add_argument("--goal", required=True)
    live.add_argument("--input", type=Path, required=True)
    live.add_argument("--approve-plan", action="store_true")
    replay = commands.add_parser("replay", help="read a persisted session without execution")
    replay.add_argument("--session", type=Path, required=True)
    replay.add_argument("--current-knowledge-version")
    return parser


def _ollama_json(system: str, prompt: str):
    return generate_json_stream(
        model=AUDIT_MODEL,
        system=system,
        prompt=prompt,
        options={"temperature": 0.1, "num_ctx": 8192, "num_keep": 0},
    )


def _rules_version() -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(CONFIG_DIR).glob("*.txt")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def build_live_runtime(
    *, workspace: Workspace, source_text: str, source_name: str
) -> tuple[AgentRuntime, str]:
    registry = ToolRegistry()
    services = {"source_text": source_text, "source_id": source_name}
    if workspace is Workspace.MEETING_AUDIT:
        collection = initialize_knowledge_base()
        services.update(
            meeting_state={},
            meeting_model=_ollama_json,
            rule_search=lambda query, top_k: retrieve_relevant_context(collection, query, top_k),
        )
        register_meeting_tools(registry)
        capability = build_meeting_capability()
        planner = MeetingPlaybookPlanner()
        knowledge_version = _rules_version()
    else:
        patents = load_corpus(CORPUS_PATH)
        collection = chromadb.Client().get_or_create_collection(
            f"synthetic_sic_patents_{uuid.uuid4().hex}"
        )

        def embed(texts):
            return [ollama.embeddings(model=EMBED_MODEL, prompt=text)["embedding"] for text in texts]

        build_semantic_index(collection, patents, embed)
        services.update(
            patent_state={},
            patent_corpus=patents,
            patent_feature_model=_ollama_json,
            patent_semantic_search=lambda queries, limit: semantic_search(collection, queries, embed, limit),
        )
        register_patent_tools(registry)
        capability = build_patent_capability()
        planner = PatentPlaybookPlanner()
        knowledge_version = corpus_version(CORPUS_PATH)
    return AgentRuntime(
        store=SessionStore(SESSIONS_DIR),
        registry=registry,
        planner=planner,
        capability=capability,
        services=services,
    ), knowledge_version


def _print_plan(workspace: Workspace, output_func: Callable[[str], None]) -> None:
    capability = build_meeting_capability() if workspace is Workspace.MEETING_AUDIT else build_patent_capability()
    output_func("Proposed Plan")
    for index, stage in enumerate(capability.stages, 1):
        optional = " (optional)" if stage not in capability.required_stages else ""
        output_func(f"  {index}. {stage}{optional}")


def _run_live(args, input_func, output_func, runtime_factory):
    workspace = Workspace(args.workspace)
    _print_plan(workspace, output_func)
    if not args.approve_plan and input_func("Approve plan? [y/N] ").strip().lower() != "y":
        output_func("Plan not approved; no session was started.")
        return 2
    try:
        source_text = args.input.read_text(encoding="utf-8")
    except OSError as exc:
        output_func(f"Input error: {exc}")
        return 2
    runtime_value = runtime_factory(workspace=workspace, source_text=source_text, source_name=args.input.name)
    if isinstance(runtime_value, tuple):
        runtime, knowledge_version = runtime_value
    else:
        runtime, knowledge_version = runtime_value, "unknown"
    session = runtime.create_session(
        goal=args.goal,
        source_name=args.input.name,
        source_sha256=source_sha256(source_text),
        model_name=AUDIT_MODEL,
        knowledge_version=knowledge_version,
    )
    runtime.approve(session.session_id)
    session = runtime.run_until_pause(session.session_id)
    if session.status is SessionStatus.WAITING_FOR_CLARIFICATION:
        response = input_func(f"{session.pending_question}\nAnswer (blank to skip): ").strip()
        if response:
            runtime.submit_clarification(session.session_id, response)
        else:
            runtime.skip_clarification(session.session_id)
        session = runtime.run_until_pause(session.session_id)
    output_func(f"[LIVE] session={session.session_id} status={session.status.value}")
    for path in session.artifact_paths:
        output_func(f"artifact: {path}")
    if session.error:
        output_func(f"error: {session.error}")
    return 0 if session.status is SessionStatus.COMPLETED else 3


def _run_replay(args, output_func):
    try:
        replay = load_replay(args.session, current_knowledge_version=args.current_knowledge_version)
    except ReplayError as exc:
        output_func(f"Replay error: {exc}")
        return 2
    output_func(f"[REPLAY] session={replay.metadata['session_id']} status={replay.metadata['status']}")
    output_func(f"model={replay.metadata['model_name']}")
    output_func(f"knowledge_version={replay.metadata['knowledge_version']}")
    if replay.knowledge_version_matches is False:
        output_func("warning: current knowledge version differs from the recorded session")
    for event in replay.events:
        output_func(f"{event.timestamp} {event.kind} {event.payload.get('summary', '')}")
    for path in replay.artifact_manifest:
        output_func(f"artifact: {path}")
    return 0


def main(
    argv=None,
    *,
    input_func=input,
    output_func=print,
    runtime_factory=build_live_runtime,
) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "live":
        return _run_live(args, input_func, output_func, runtime_factory)
    return _run_replay(args, output_func)


if __name__ == "__main__":
    sys.exit(main())
