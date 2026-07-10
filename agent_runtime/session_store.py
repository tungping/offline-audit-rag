import json
import os
import re
from pathlib import Path
from typing import Any

import audit_rules

from .models import AgentEvent, AgentSession, Evidence


SESSION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return audit_rules.mask_sensitive_evidence(value)
    if isinstance(value, dict):
        return {str(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item) for item in value]
    return value


class SessionStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, session: AgentSession) -> Path:
        session_dir = self._session_dir(session.session_id)
        session_dir.mkdir(parents=False, exist_ok=False)
        (session_dir / "artifacts").mkdir()
        request = {
            "session_id": session.session_id,
            "workspace": session.workspace.value,
            "goal": session.goal,
            "source_name": session.source_name,
            "source_sha256": session.source_sha256,
            "created_at": session.created_at,
        }
        self._atomic_json(session_dir / "request.json", request)
        self._atomic_json(session_dir / "session.json", session.to_dict())
        self._atomic_json(session_dir / "evidence.json", [])
        (session_dir / "events.jsonl").write_text("", encoding="utf-8")
        return session_dir

    def load(self, session_id: str) -> AgentSession:
        path = self._session_dir(session_id) / "session.json"
        return AgentSession.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, session: AgentSession) -> None:
        self._atomic_json(
            self._session_dir(session.session_id) / "session.json",
            session.to_dict(),
        )

    def append_event(self, session_id: str, event: AgentEvent) -> None:
        path = self._session_dir(session_id) / "events.jsonl"
        payload = json.dumps(
            _sanitize(event.to_dict()), ensure_ascii=False, sort_keys=True
        )
        with path.open("a", encoding="utf-8") as event_file:
            event_file.write(payload + "\n")

    def save_evidence(
        self, session_id: str, evidence_list: list[Evidence]
    ) -> None:
        self._atomic_json(
            self._session_dir(session_id) / "evidence.json",
            [evidence.to_dict() for evidence in evidence_list],
        )

    def load_evidence(self, session_id: str) -> list[Evidence]:
        path = self._session_dir(session_id) / "evidence.json"
        values = json.loads(path.read_text(encoding="utf-8"))
        return [Evidence.from_dict(value) for value in values]

    def artifact_dir(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "artifacts"

    def _session_dir(self, session_id: str) -> Path:
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            raise ValueError("invalid session id")
        return self.root / session_id

    @staticmethod
    def _atomic_json(path: Path, value: Any) -> None:
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(
                _sanitize(value), ensure_ascii=False, indent=2, sort_keys=True
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
