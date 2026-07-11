import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import audit_rules

from .models import AgentSession, SessionStatus, Workspace
from .replay import ReplayError, load_replay


EVIDENCE_ID_PATTERN = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")
REQUIRED_ARTIFACTS = {
    Workspace.MEETING_AUDIT: frozenset(
        {"meeting_audit_report.md", "tasks.csv", "risk_items.csv"}
    ),
    Workspace.PATENT_RESEARCH: frozenset(
        {
            "patent_research_report.md",
            "patent_retrieval_results.csv",
            "claim_chart.csv",
        }
    ),
}


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationReport:
    session_id: str
    workspace: str
    status: str
    checked_artifacts: tuple[str, ...]
    errors: tuple[ValidationIssue, ...]
    warnings: tuple[ValidationIssue, ...]

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "workspace": self.workspace,
            "status": self.status,
            "checked_artifacts": list(self.checked_artifacts),
            "valid": self.valid,
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
        }


def _failure(code: str, message: str) -> ValidationIssue:
    return ValidationIssue(code=code, message=message)


def validate_session_bundle(session_dir: Path | str) -> ValidationReport:
    path = Path(session_dir).resolve()
    try:
        session = AgentSession.from_dict(
            json.loads((path / "session.json").read_text(encoding="utf-8"))
        )
        replay = load_replay(path)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, ReplayError) as exc:
        return ValidationReport(
            session_id="",
            workspace="",
            status="",
            checked_artifacts=(),
            errors=(_failure("malformed_bundle", str(exc)),),
            warnings=(),
        )

    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
    artifact_root = (path / "artifacts").resolve()
    checked: list[str] = []
    existing_names: set[str] = set()
    artifact_texts: list[tuple[Path, str]] = []

    for raw_artifact in replay.artifact_manifest:
        artifact = Path(raw_artifact).resolve()
        try:
            artifact.relative_to(artifact_root)
        except ValueError:
            errors.append(
                _failure("artifact_escape", f"artifact escapes session: {raw_artifact}")
            )
            continue
        checked.append(str(artifact))
        if not artifact.is_file():
            warnings.append(
                _failure("missing_recorded_artifact", f"artifact no longer exists: {artifact}")
            )
            continue
        existing_names.add(artifact.name)
        try:
            artifact_texts.append((artifact, artifact.read_text(encoding="utf-8-sig")))
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(_failure("unreadable_artifact", f"{artifact}: {exc}"))

    if session.status is SessionStatus.COMPLETED:
        missing = REQUIRED_ARTIFACTS[session.workspace] - existing_names
        for name in sorted(missing):
            errors.append(_failure("missing_artifact", f"completed session lacks {name}"))
    if session.status is SessionStatus.INCOMPLETE and session.workspace is Workspace.PATENT_RESEARCH:
        completed_report = artifact_root / "patent_research_report.md"
        if completed_report.exists():
            errors.append(
                _failure(
                    "false_completion",
                    "incomplete patent session contains a completed report",
                )
            )

    counters = {
        "model_calls": (session.model_calls, session.budget.max_model_calls),
        "tool_calls": (session.tool_calls, session.budget.max_tool_calls),
        "query_rounds": (session.query_rounds, session.budget.max_query_rounds),
        "clarification_rounds": (
            session.clarification_rounds,
            session.budget.max_clarifications,
        ),
    }
    for label, (actual, maximum) in counters.items():
        if actual > maximum:
            errors.append(
                _failure("budget_exceeded", f"{label}={actual} exceeds {maximum}")
            )

    known_evidence = {item.evidence_id for item in replay.evidence}
    for artifact, text in artifact_texts:
        for evidence_id in EVIDENCE_ID_PATTERN.findall(text):
            if evidence_id not in known_evidence:
                errors.append(
                    _failure(
                        "dangling_evidence",
                        f"{artifact.name} references unknown evidence {evidence_id}",
                    )
                )

    if session.workspace is Workspace.MEETING_AUDIT:
        persisted_paths = [path / "request.json", path / "session.json", path / "evidence.json"]
        persisted_paths.extend(artifact for artifact, _ in artifact_texts)
        for persisted_path in persisted_paths:
            try:
                text = persisted_path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError):
                continue
            if audit_rules.MOBILE_PATTERN.search(text) or audit_rules.EMAIL_PATTERN.search(text):
                errors.append(
                    _failure(
                        "sensitive_data",
                        f"unmasked sensitive data in {persisted_path.name}",
                    )
                )

    if session.status not in {
        SessionStatus.COMPLETED,
        SessionStatus.INCOMPLETE,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
    }:
        warnings.append(
            _failure("non_terminal", f"session status is {session.status.value}")
        )

    return ValidationReport(
        session_id=session.session_id,
        workspace=session.workspace.value,
        status=session.status.value,
        checked_artifacts=tuple(checked),
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
