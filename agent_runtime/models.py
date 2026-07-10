import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class Workspace(str, Enum):
    MEETING_AUDIT = "meeting_audit"
    PATENT_RESEARCH = "patent_research"


class SessionStatus(str, Enum):
    DRAFT_PLAN = "DRAFT_PLAN"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    RUNNING = "RUNNING"
    WAITING_FOR_CLARIFICATION = "WAITING_FOR_CLARIFICATION"
    VERIFYING = "VERIFYING"
    GENERATING_ARTIFACTS = "GENERATING_ARTIFACTS"
    COMPLETED = "COMPLETED"
    INCOMPLETE = "INCOMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ActionKind(str, Enum):
    TOOL_CALL = "TOOL_CALL"
    REQUEST_CLARIFICATION = "REQUEST_CLARIFICATION"
    ADVANCE_STAGE = "ADVANCE_STAGE"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True)
class ResourceBudget:
    max_model_calls: int
    max_tool_calls: int
    max_query_rounds: int = 2
    max_clarifications: int = 1
    max_tool_retries: int = 1

    def __post_init__(self) -> None:
        values = (
            self.max_model_calls,
            self.max_tool_calls,
            self.max_query_rounds,
            self.max_clarifications,
            self.max_tool_retries,
        )
        if any(value <= 0 for value in values):
            raise ValueError("resource budget values must be greater than zero")

    @classmethod
    def meeting_default(cls) -> "ResourceBudget":
        return cls(max_model_calls=4, max_tool_calls=10)

    @classmethod
    def patent_default(cls) -> "ResourceBudget":
        return cls(max_model_calls=5, max_tool_calls=14)

    def to_dict(self) -> dict[str, int]:
        return {
            "max_model_calls": self.max_model_calls,
            "max_tool_calls": self.max_tool_calls,
            "max_query_rounds": self.max_query_rounds,
            "max_clarifications": self.max_clarifications,
            "max_tool_retries": self.max_tool_retries,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResourceBudget":
        return cls(**{key: int(value) for key, value in data.items()})


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    stage: str
    title: str
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "stage": self.stage,
            "title": self.title,
            "required": self.required,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanStep":
        return cls(
            step_id=str(data["step_id"]),
            stage=str(data["stage"]),
            title=str(data["title"]),
            required=bool(data.get("required", True)),
        )


@dataclass(frozen=True)
class AgentAction:
    kind: ActionKind
    reason_summary: str
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    next_stage: str = ""
    question: str = ""

    @classmethod
    def tool(
        cls,
        tool_name: str,
        arguments: dict[str, Any],
        reason_summary: str = "执行当前阶段工具",
    ) -> "AgentAction":
        return cls(
            kind=ActionKind.TOOL_CALL,
            reason_summary=reason_summary,
            tool_name=tool_name,
            arguments=arguments,
        )

    @classmethod
    def clarification(
        cls, question: str, reason_summary: str = "需要用户补充信息"
    ) -> "AgentAction":
        return cls(
            kind=ActionKind.REQUEST_CLARIFICATION,
            reason_summary=reason_summary,
            question=question,
        )

    @classmethod
    def advance(
        cls, next_stage: str, reason_summary: str = "进入下一阶段"
    ) -> "AgentAction":
        return cls(
            kind=ActionKind.ADVANCE_STAGE,
            reason_summary=reason_summary,
            next_stage=next_stage,
        )

    @classmethod
    def complete(cls, reason_summary: str = "任务完成") -> "AgentAction":
        return cls(kind=ActionKind.COMPLETE, reason_summary=reason_summary)


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    source_type: str
    source_id: str
    locator: str
    quote: str
    source_sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "evidence_id": self.evidence_id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "locator": self.locator,
            "quote": self.quote,
            "source_sha256": self.source_sha256,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Evidence":
        return cls(**{key: str(value) for key, value in data.items()})


@dataclass(frozen=True)
class AgentEvent:
    event_id: str
    timestamp: str
    kind: str
    stage: str
    payload: dict[str, Any]

    @classmethod
    def status(cls, summary: str, stage: str = "") -> "AgentEvent":
        return cls(
            event_id=uuid.uuid4().hex,
            timestamp=datetime.now(timezone.utc).isoformat(),
            kind="status",
            stage=stage,
            payload={"summary": summary},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "kind": self.kind,
            "stage": self.stage,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentEvent":
        return cls(
            event_id=str(data["event_id"]),
            timestamp=str(data["timestamp"]),
            kind=str(data["kind"]),
            stage=str(data.get("stage", "")),
            payload=dict(data.get("payload", {})),
        )


@dataclass
class AgentSession:
    session_id: str
    workspace: Workspace
    goal: str
    source_name: str
    source_sha256: str
    model_name: str
    knowledge_version: str
    budget: ResourceBudget
    status: SessionStatus
    current_stage: str
    plan: list[PlanStep]
    completed_stages: list[str]
    model_calls: int
    tool_calls: int
    query_rounds: int
    clarification_rounds: int
    artifact_paths: list[str]
    error: str
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        if not self.goal.strip():
            raise ValueError("goal must not be empty")
        if not self.source_name.strip():
            raise ValueError("source name must not be empty")
        if not SHA256_PATTERN.fullmatch(self.source_sha256):
            raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
        counters = (
            self.model_calls,
            self.tool_calls,
            self.query_rounds,
            self.clarification_rounds,
        )
        if any(value < 0 for value in counters):
            raise ValueError("session counters must not be negative")

    @classmethod
    def new(
        cls,
        *,
        workspace: Workspace,
        goal: str,
        source_name: str,
        source_sha256: str,
        model_name: str,
        knowledge_version: str,
        budget: ResourceBudget,
    ) -> "AgentSession":
        now = datetime.now(timezone.utc).isoformat()
        return cls(
            session_id=uuid.uuid4().hex,
            workspace=workspace,
            goal=goal.strip(),
            source_name=source_name,
            source_sha256=source_sha256,
            model_name=model_name,
            knowledge_version=knowledge_version,
            budget=budget,
            status=SessionStatus.DRAFT_PLAN,
            current_stage="",
            plan=[],
            completed_stages=[],
            model_calls=0,
            tool_calls=0,
            query_rounds=0,
            clarification_rounds=0,
            artifact_paths=[],
            error="",
            created_at=now,
            updated_at=now,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace": self.workspace.value,
            "goal": self.goal,
            "source_name": self.source_name,
            "source_sha256": self.source_sha256,
            "model_name": self.model_name,
            "knowledge_version": self.knowledge_version,
            "budget": self.budget.to_dict(),
            "status": self.status.value,
            "current_stage": self.current_stage,
            "plan": [step.to_dict() for step in self.plan],
            "completed_stages": list(self.completed_stages),
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "query_rounds": self.query_rounds,
            "clarification_rounds": self.clarification_rounds,
            "artifact_paths": list(self.artifact_paths),
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentSession":
        return cls(
            session_id=str(data["session_id"]),
            workspace=Workspace(str(data["workspace"])),
            goal=str(data["goal"]),
            source_name=str(data["source_name"]),
            source_sha256=str(data["source_sha256"]),
            model_name=str(data["model_name"]),
            knowledge_version=str(data["knowledge_version"]),
            budget=ResourceBudget.from_dict(dict(data["budget"])),
            status=SessionStatus(str(data["status"])),
            current_stage=str(data.get("current_stage", "")),
            plan=[PlanStep.from_dict(item) for item in data.get("plan", [])],
            completed_stages=[str(item) for item in data.get("completed_stages", [])],
            model_calls=int(data.get("model_calls", 0)),
            tool_calls=int(data.get("tool_calls", 0)),
            query_rounds=int(data.get("query_rounds", 0)),
            clarification_rounds=int(data.get("clarification_rounds", 0)),
            artifact_paths=[str(item) for item in data.get("artifact_paths", [])],
            error=str(data.get("error", "")),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
        )
