from agent_runtime.models import AgentAction, ResourceBudget, Workspace
from agent_runtime.planner import CapabilityDefinition, PlannedAction


def _observations(session, tool_name):
    return [item for item in session.observations if item.get("tool_name") == tool_name]


def _latest_candidates(session):
    observations = _observations(session, "patent.semantic_search")
    return observations[-1].get("data", {}).get("candidates", []) if observations else []


class PatentPlaybookPlanner:
    """Deterministic bounded orchestration for the synthetic corpus demo."""

    def next_action(self, session, capability):
        stage = session.current_stage
        if stage == "extract_features":
            return self._once_then_advance(session, "patent.extract_features", "initial_search")
        if stage in {"initial_search", "adjust_search"}:
            if stage == "adjust_search" and _latest_candidates(session):
                return PlannedAction(AgentAction.advance("read_candidates"), 0)
            target_count = 1 if stage == "initial_search" else 2
            if len(_observations(session, "patent.keyword_search")) < target_count:
                return PlannedAction(AgentAction.tool("patent.keyword_search", {}), 0)
            if len(_observations(session, "patent.semantic_search")) < target_count:
                return PlannedAction(AgentAction.tool("patent.semantic_search", {}), 0)
            next_stage = "adjust_search" if stage == "initial_search" else "read_candidates"
            return PlannedAction(AgentAction.advance(next_stage), 0)
        if stage == "read_candidates":
            read_ids = {item.get("data", {}).get("document_id") for item in _observations(session, "patent.read_candidate")}
            for document_id in _latest_candidates(session)[:5]:
                if document_id not in read_ids:
                    return PlannedAction(AgentAction.tool("patent.read_candidate", {"document_id": document_id}), 0)
            return PlannedAction(AgentAction.advance("compare_claims"), 0)
        if stage == "compare_claims":
            return self._once_then_advance(session, "patent.compare_claims", "verify")
        if stage == "verify":
            return self._once_then_advance(session, "patent.verify_evidence", "artifacts")
        if stage == "artifacts":
            if not _observations(session, "patent.write_artifacts"):
                return PlannedAction(AgentAction.tool("patent.write_artifacts", {}), 0)
            return PlannedAction(AgentAction.complete(), 0)
        raise ValueError(f"unsupported patent stage: {stage}")

    @staticmethod
    def _once_then_advance(session, tool_name, next_stage):
        if not _observations(session, tool_name):
            return PlannedAction(AgentAction.tool(tool_name, {}), 0)
        return PlannedAction(AgentAction.advance(next_stage), 0)


def build_patent_capability():
    stages = ("extract_features", "initial_search", "adjust_search", "read_candidates", "compare_claims", "verify", "artifacts")
    return CapabilityDefinition(
        workspace=Workspace.PATENT_RESEARCH,
        display_name="Synthetic SiC Patent Research",
        stages=stages,
        stage_tools={
            "extract_features": frozenset({"patent.extract_features"}),
            "initial_search": frozenset({"patent.keyword_search", "patent.semantic_search"}),
            "adjust_search": frozenset({"patent.keyword_search", "patent.semantic_search"}),
            "read_candidates": frozenset({"patent.read_candidate"}),
            "compare_claims": frozenset({"patent.compare_claims"}),
            "verify": frozenset({"patent.verify_evidence"}),
            "artifacts": frozenset({"patent.write_artifacts"}),
        },
        required_stages=frozenset({"extract_features", "initial_search", "read_candidates", "compare_claims", "verify", "artifacts"}),
        budget=ResourceBudget.patent_default(),
        planner_system_prompt="Follow the bounded synthetic patent research playbook.",
    )
