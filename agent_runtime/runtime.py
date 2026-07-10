import uuid
from datetime import datetime, timezone
from typing import Any

from .models import (
    AgentAction,
    AgentEvent,
    AgentSession,
    ActionKind,
    PlanStep,
    SessionStatus,
)
from .planner import ActionPlanner, CapabilityDefinition, PlannerError
from .session_store import SessionStore
from .tools import ToolContext, ToolExecutionError, ToolRegistry


ALLOWED_TRANSITIONS = {
    SessionStatus.DRAFT_PLAN: {SessionStatus.WAITING_FOR_APPROVAL},
    SessionStatus.WAITING_FOR_APPROVAL: {
        SessionStatus.RUNNING,
        SessionStatus.CANCELLED,
    },
    SessionStatus.RUNNING: {
        SessionStatus.WAITING_FOR_CLARIFICATION,
        SessionStatus.VERIFYING,
        SessionStatus.INCOMPLETE,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
    },
    SessionStatus.WAITING_FOR_CLARIFICATION: {
        SessionStatus.RUNNING,
        SessionStatus.INCOMPLETE,
        SessionStatus.CANCELLED,
    },
    SessionStatus.VERIFYING: {
        SessionStatus.GENERATING_ARTIFACTS,
        SessionStatus.INCOMPLETE,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
    },
    SessionStatus.GENERATING_ARTIFACTS: {
        SessionStatus.COMPLETED,
        SessionStatus.INCOMPLETE,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
    },
}

ACTIVE_STATUSES = {
    SessionStatus.RUNNING,
    SessionStatus.VERIFYING,
    SessionStatus.GENERATING_ARTIFACTS,
}
TERMINAL_STATUSES = {
    SessionStatus.COMPLETED,
    SessionStatus.INCOMPLETE,
    SessionStatus.FAILED,
    SessionStatus.CANCELLED,
}


class RuntimeStateError(ValueError):
    """Raised when a caller requests an illegal runtime transition."""


class AgentRuntime:
    def __init__(
        self,
        *,
        store: SessionStore,
        registry: ToolRegistry,
        planner: ActionPlanner,
        capability: CapabilityDefinition,
        services: dict[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.planner = planner
        self.capability = capability
        self.services = dict(services or {})

    def create_session(
        self,
        *,
        goal: str,
        source_name: str,
        source_sha256: str,
        model_name: str,
        knowledge_version: str,
    ) -> AgentSession:
        session = AgentSession.new(
            workspace=self.capability.workspace,
            goal=goal,
            source_name=source_name,
            source_sha256=source_sha256,
            model_name=model_name,
            knowledge_version=knowledge_version,
            budget=self.capability.budget,
        )
        session.current_stage = self.capability.stages[0]
        session.plan = [
            PlanStep(
                step_id=f"step-{index + 1}",
                stage=stage,
                title=stage.replace("_", " ").title(),
                required=stage in self.capability.required_stages,
            )
            for index, stage in enumerate(self.capability.stages)
        ]
        self.store.create(session)
        self._transition(session, SessionStatus.WAITING_FOR_APPROVAL)
        return session

    def approve(self, session_id: str) -> AgentSession:
        session = self.store.load(session_id)
        if session.status is not SessionStatus.WAITING_FOR_APPROVAL:
            raise RuntimeStateError("session is not waiting for approval")
        self._transition(session, SessionStatus.RUNNING)
        return session

    def run_until_pause(
        self,
        session_id: str,
        *,
        cancel_checker=None,
    ) -> AgentSession:
        session = self.store.load(session_id)
        if session.status not in ACTIVE_STATUSES:
            raise RuntimeStateError("session is not runnable")
        is_cancelled = cancel_checker or (lambda: False)

        while session.status in ACTIVE_STATUSES:
            if is_cancelled():
                return self.cancel(session.session_id)
            if session.model_calls >= session.budget.max_model_calls:
                return self._incomplete(session, "model call budget exhausted")

            try:
                decision = self.planner.next_action(session, self.capability)
            except PlannerError as exc:
                session.model_calls += exc.model_calls
                self.store.save(session)
                return self._fail(session, str(exc))
            except Exception as exc:
                session.model_calls += 1
                self.store.save(session)
                return self._fail(session, f"planner failed: {exc}")

            session.model_calls += decision.model_calls
            self.store.save(session)
            if session.model_calls > session.budget.max_model_calls:
                return self._incomplete(session, "model call budget exhausted")

            action = decision.action
            if action.kind is ActionKind.TOOL_CALL:
                result = self._run_tool(session, action, is_cancelled)
                if result is not None:
                    return result
                continue
            if action.kind is ActionKind.REQUEST_CLARIFICATION:
                if (
                    session.clarification_rounds
                    >= session.budget.max_clarifications
                ):
                    return self._incomplete(
                        session, "clarification budget exhausted"
                    )
                session.clarification_rounds += 1
                session.pending_question = action.question
                self.store.save(session)
                self._append_event(
                    session,
                    "clarification_requested",
                    {
                        "summary": action.reason_summary,
                        "question": action.question,
                    },
                )
                self._transition(
                    session, SessionStatus.WAITING_FOR_CLARIFICATION
                )
                return session
            if action.kind is ActionKind.ADVANCE_STAGE:
                failure = self._advance_stage(session, action)
                if failure is not None:
                    return failure
                continue
            if action.kind is ActionKind.COMPLETE:
                return self._complete(session, action)
            return self._fail(session, "unsupported action kind")

        return session

    def submit_clarification(
        self, session_id: str, response: str
    ) -> AgentSession:
        session = self.store.load(session_id)
        if session.status is not SessionStatus.WAITING_FOR_CLARIFICATION:
            raise RuntimeStateError("session is not waiting for clarification")
        session.clarification_response = response.strip()
        session.observations.append(
            {
                "kind": "clarification",
                "question": session.pending_question,
                "response": response.strip(),
            }
        )
        session.pending_question = ""
        self.store.save(session)
        self._append_event(
            session,
            "clarification_received",
            {"summary": "用户已提交澄清信息"},
        )
        self._transition(session, SessionStatus.RUNNING)
        return session

    def skip_clarification(self, session_id: str) -> AgentSession:
        session = self.store.load(session_id)
        if session.status is not SessionStatus.WAITING_FOR_CLARIFICATION:
            raise RuntimeStateError("session is not waiting for clarification")
        session.observations.append(
            {
                "kind": "clarification",
                "question": session.pending_question,
                "response": "",
                "skipped": True,
            }
        )
        session.pending_question = ""
        self.store.save(session)
        self._append_event(
            session,
            "clarification_skipped",
            {"summary": "用户选择跳过澄清"},
        )
        self._transition(session, SessionStatus.RUNNING)
        return session

    def cancel(self, session_id: str) -> AgentSession:
        session = self.store.load(session_id)
        if session.status in TERMINAL_STATUSES:
            return session
        self._transition(session, SessionStatus.CANCELLED)
        return session

    def _run_tool(
        self,
        session: AgentSession,
        action: AgentAction,
        cancel_checker,
    ) -> AgentSession | None:
        allowed = self.capability.stage_tools.get(
            session.current_stage, frozenset()
        )
        if action.tool_name not in allowed:
            return self._fail(
                session,
                f"tool {action.tool_name} is not allowed in stage {session.current_stage}",
            )
        retry_count = 0
        while True:
            if session.tool_calls >= session.budget.max_tool_calls:
                return self._incomplete(session, "tool call budget exhausted")
            session.tool_calls += 1
            self.store.save(session)
            try:
                result = self.registry.execute(
                    action.tool_name,
                    action.arguments,
                    ToolContext(
                        session=session,
                        session_dir=self.store.artifact_dir(
                            session.session_id
                        ).parent,
                        cancel_checker=cancel_checker,
                        services=self.services,
                    ),
                )
                break
            except InterruptedError:
                return self.cancel(session.session_id)
            except ToolExecutionError as exc:
                if (
                    not exc.retryable
                    or retry_count >= session.budget.max_tool_retries
                ):
                    return self._fail(session, f"tool failed: {exc}")
                retry_count += 1
                self._append_event(
                    session,
                    "tool_retry",
                    {
                        "tool_name": action.tool_name,
                        "summary": str(exc),
                        "retry": retry_count,
                    },
                )
            except Exception as exc:
                return self._fail(session, f"tool failed: {exc}")

        evidence = self.store.load_evidence(session.session_id)
        known_ids = {item.evidence_id for item in evidence}
        evidence.extend(
            item for item in result.evidence if item.evidence_id not in known_ids
        )
        self.store.save_evidence(session.session_id, evidence)
        session.model_calls += result.model_calls
        session.query_rounds += result.query_rounds
        session.observations.append(
            {
                "tool_name": action.tool_name,
                "summary": result.summary,
                "data": result.data,
                "evidence_ids": [item.evidence_id for item in result.evidence],
            }
        )
        self.store.save(session)
        self._append_event(
            session,
            "tool_result",
            {
                "tool_name": action.tool_name,
                "summary": result.summary,
                "evidence_ids": [item.evidence_id for item in result.evidence],
            },
        )
        if session.model_calls > session.budget.max_model_calls:
            return self._incomplete(session, "model call budget exhausted")
        if session.query_rounds > session.budget.max_query_rounds:
            return self._incomplete(session, "query adjustment budget exhausted")
        if (
            result.query_rounds
            and session.query_rounds >= session.budget.max_query_rounds
            and result.data.get("no_results") is True
        ):
            return self._incomplete(session, "no results after query budget")
        return None

    def _advance_stage(
        self, session: AgentSession, action: AgentAction
    ) -> AgentSession | None:
        stages = self.capability.stages
        current_index = stages.index(session.current_stage)
        if current_index + 1 >= len(stages):
            return self._fail(session, "no later stage is available")
        expected = stages[current_index + 1]
        if action.next_stage != expected:
            return self._fail(
                session,
                f"next stage must be {expected}, got {action.next_stage}",
            )
        if session.current_stage not in session.completed_stages:
            session.completed_stages.append(session.current_stage)
        session.current_stage = expected
        self.store.save(session)
        self._append_event(
            session,
            "stage_advanced",
            {"summary": action.reason_summary, "stage": expected},
        )
        target_status = session.status
        if expected == "verify":
            target_status = SessionStatus.VERIFYING
        elif expected == "artifacts":
            target_status = SessionStatus.GENERATING_ARTIFACTS
        if target_status is not session.status:
            self._transition(session, target_status)
        return None

    def _complete(
        self, session: AgentSession, action: AgentAction
    ) -> AgentSession:
        completed = set(session.completed_stages) | {session.current_stage}
        missing = self.capability.required_stages - completed
        if missing or session.status is not SessionStatus.GENERATING_ARTIFACTS:
            return self._fail(
                session,
                f"required stages are incomplete: {sorted(missing)}",
            )
        if session.current_stage not in session.completed_stages:
            session.completed_stages.append(session.current_stage)
        self.store.save(session)
        self._append_event(
            session, "completed", {"summary": action.reason_summary}
        )
        self._transition(session, SessionStatus.COMPLETED)
        return session

    def _fail(self, session: AgentSession, message: str) -> AgentSession:
        session.error = message
        self.store.save(session)
        self._transition(session, SessionStatus.FAILED)
        return session

    def _incomplete(self, session: AgentSession, message: str) -> AgentSession:
        session.error = message
        self.store.save(session)
        self._transition(session, SessionStatus.INCOMPLETE)
        return session

    def _transition(
        self, session: AgentSession, target: SessionStatus
    ) -> None:
        allowed = ALLOWED_TRANSITIONS.get(session.status, set())
        if target not in allowed:
            raise RuntimeStateError(
                f"illegal transition {session.status.value} -> {target.value}"
            )
        previous = session.status
        session.status = target
        session.updated_at = datetime.now(timezone.utc).isoformat()
        self.store.save(session)
        self._append_event(
            session,
            "status_changed",
            {"from": previous.value, "to": target.value},
        )

    def _append_event(
        self, session: AgentSession, kind: str, payload: dict[str, Any]
    ) -> None:
        self.store.append_event(
            session.session_id,
            AgentEvent(
                event_id=uuid.uuid4().hex,
                timestamp=datetime.now(timezone.utc).isoformat(),
                kind=kind,
                stage=session.current_stage,
                payload=payload,
            ),
        )
