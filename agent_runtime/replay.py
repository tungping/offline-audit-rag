import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .models import AgentEvent, AgentSession, Evidence


class ReplayError(ValueError):
    """Raised when a persisted session cannot be replayed safely."""


@dataclass(frozen=True)
class ReplaySession:
    mode: str
    metadata: dict[str, Any]
    events: tuple[AgentEvent, ...]
    evidence: tuple[Evidence, ...]
    artifact_manifest: tuple[str, ...]
    knowledge_version_matches: bool | None


def _read_json(path: Path, label: str):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise ReplayError(f"malformed {label}: {exc}") from exc


def load_replay(
    session_dir: Path | str,
    *,
    current_knowledge_version: str | None = None,
) -> ReplaySession:
    raw_path = Path(session_dir)
    if ".." in raw_path.parts:
        raise ReplayError("path traversal is not allowed")
    path = raw_path.resolve()
    if not path.is_dir():
        raise ReplayError("session directory does not exist")
    try:
        session = AgentSession.from_dict(_read_json(path / "session.json", "session.json"))
        evidence_raw = _read_json(path / "evidence.json", "evidence.json")
        evidence = tuple(Evidence.from_dict(item) for item in evidence_raw)
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ReplayError):
            raise
        raise ReplayError(f"malformed session bundle: {exc}") from exc

    events = []
    try:
        with (path / "events.jsonl").open(encoding="utf-8") as event_file:
            for line_number, line in enumerate(event_file, 1):
                if not line.strip():
                    continue
                try:
                    events.append(AgentEvent.from_dict(json.loads(line)))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    raise ReplayError(
                        f"malformed events.jsonl line {line_number}: {exc}"
                    ) from exc
    except OSError as exc:
        raise ReplayError(f"malformed events.jsonl: {exc}") from exc

    artifact_paths = list(session.artifact_paths)
    if not artifact_paths:
        for observation in session.observations:
            paths = observation.get("data", {}).get("artifact_paths", [])
            artifact_paths.extend(str(item) for item in paths)
    matches = (
        None
        if current_knowledge_version is None
        else current_knowledge_version == session.knowledge_version
    )
    metadata = {
        "session_id": session.session_id,
        "workspace": session.workspace.value,
        "status": session.status.value,
        "goal": session.goal,
        "source_name": session.source_name,
        "model_name": session.model_name,
        "knowledge_version": session.knowledge_version,
        "model_calls": session.model_calls,
        "tool_calls": session.tool_calls,
        "query_rounds": session.query_rounds,
    }
    return ReplaySession(
        mode="REPLAY",
        metadata=metadata,
        events=tuple(events),
        evidence=evidence,
        artifact_manifest=tuple(dict.fromkeys(artifact_paths)),
        knowledge_version_matches=matches,
    )


def iter_replay_events(replay: ReplaySession) -> Iterator[AgentEvent]:
    yield from replay.events
