from agent_runtime.models import AgentAction, ResourceBudget, Workspace
from agent_runtime.planner import CapabilityDefinition, PlannedAction


STAGE_TOOL = {
    "parse": "meeting.extract_structure",
    "retrieve_rules": "meeting.search_rules",
    "risk_checks": "meeting.run_rule_checks",
    "verify": "meeting.verify_evidence",
    "artifacts": "meeting.write_artifacts",
}


def _has_tool_result(session, tool_name: str) -> bool:
    return any(item.get("tool_name") == tool_name for item in session.observations)


def _clarification_payload(session):
    for item in reversed(session.observations):
        if item.get("tool_name") == "meeting.run_rule_checks":
            return item.get("data", {}).get("clarification", {})
    return {}


class MeetingPlaybookPlanner:
    """Deterministic orchestration; model calls are confined to scoped tools."""

    def next_action(self, session, capability) -> PlannedAction:
        stage = session.current_stage
        if stage in STAGE_TOOL:
            tool_name = STAGE_TOOL[stage]
            if not _has_tool_result(session, tool_name):
                arguments = (
                    {"query": session.goal, "top_k": 3}
                    if tool_name == "meeting.search_rules"
                    else {}
                )
                return PlannedAction(
                    AgentAction.tool(tool_name, arguments), model_calls=0
                )
            if stage == "artifacts":
                return PlannedAction(AgentAction.complete(), model_calls=0)
            next_stage = capability.stages[capability.stages.index(stage) + 1]
            return PlannedAction(AgentAction.advance(next_stage), model_calls=0)

        if stage == "clarification":
            questions = _clarification_payload(session).get("questions", [])
            answered = any(
                item.get("kind") == "clarification"
                for item in session.observations
            )
            if questions and not answered:
                question = "\n".join(item["question"] for item in questions[:5])
                return PlannedAction(
                    AgentAction.clarification(question), model_calls=0
                )
            return PlannedAction(AgentAction.advance("verify"), model_calls=0)

        raise ValueError(f"unsupported meeting stage: {stage}")


def build_meeting_capability() -> CapabilityDefinition:
    stages = (
        "parse",
        "retrieve_rules",
        "risk_checks",
        "clarification",
        "verify",
        "artifacts",
    )
    return CapabilityDefinition(
        workspace=Workspace.MEETING_AUDIT,
        display_name="Technical Project Meeting Audit",
        stages=stages,
        stage_tools={
            "parse": frozenset({"meeting.extract_structure"}),
            "retrieve_rules": frozenset({"meeting.search_rules"}),
            "risk_checks": frozenset({"meeting.run_rule_checks"}),
            "clarification": frozenset(),
            "verify": frozenset({"meeting.verify_evidence"}),
            "artifacts": frozenset({"meeting.write_artifacts"}),
        },
        required_stages=frozenset(
            {"parse", "retrieve_rules", "risk_checks", "verify", "artifacts"}
        ),
        budget=ResourceBudget.meeting_default(),
        planner_system_prompt="Follow the bounded meeting audit playbook.",
    )
