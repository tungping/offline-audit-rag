from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MeetingTask:
    task_name: str
    owner: str
    priority: str
    due_date: str
    acceptance_criteria: str
    confidence: str
    needs_human_review: bool
    evidence_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_name": self.task_name,
            "owner": self.owner,
            "priority": self.priority,
            "due_date": self.due_date,
            "acceptance_criteria": self.acceptance_criteria,
            "confidence": self.confidence,
            "needs_human_review": self.needs_human_review,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True)
class MeetingFinding:
    risk_type: str
    severity: str
    summary: str
    recommendation: str
    needs_human_review: bool
    evidence_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_type": self.risk_type,
            "severity": self.severity,
            "summary": self.summary,
            "recommendation": self.recommendation,
            "needs_human_review": self.needs_human_review,
            "evidence_ids": list(self.evidence_ids),
        }
