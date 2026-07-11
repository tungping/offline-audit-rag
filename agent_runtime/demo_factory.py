from pathlib import Path

from capabilities.meeting_audit.playbook import (
    MeetingPlaybookPlanner,
    build_meeting_capability,
)
from capabilities.meeting_audit.tools import register_meeting_tools
from capabilities.patent_research.corpus import corpus_version, load_corpus
from capabilities.patent_research.playbook import (
    PatentPlaybookPlanner,
    build_patent_capability,
)
from capabilities.patent_research.search import PatentHit, keyword_search
from capabilities.patent_research.tools import register_patent_tools

from .models import Workspace
from .runtime import AgentRuntime
from .session_store import SessionStore
from .tools import ToolRegistry


CORPUS_PATH = Path("capabilities/patent_research/corpus/synthetic_sic_patents.jsonl")


def _meeting_result():
    return {
        "decisions": [
            {
                "summary": "不经过 QA 直接发布",
                "evidence_quote": "张三建议不经过 QA，今天直接把版本推到 main。",
            }
        ],
        "tasks": [
            {
                "task_name": "修复导出脚本",
                "owner": "Unassigned",
                "priority": "High",
                "due_date": "",
                "acceptance_criteria": "",
                "evidence_quote": "研发后续尽快修复导出脚本，相关人员负责。",
            }
        ],
    }


def _patent_result(source_text: str):
    quote = (
        "沟槽底部设置与源极相连、保持接地电位的屏蔽区，"
        "用于降低栅介质承受的电场并改善长期可靠性"
    )
    if quote not in source_text:
        raise ValueError("deterministic patent fixture requires the demo product brief")
    return {
        "technical_features": [
            {
                "feature_id": "F1",
                "feature": "沟槽底部接地屏蔽区",
                "synonyms": ["底部屏蔽电极", "source-connected shield"],
                "evidence_quote": quote,
            }
        ],
        "keyword_queries": [["沟槽", "底部屏蔽区"], ["栅介质电场", "可靠性"]],
        "semantic_queries": ["降低 SiC 沟槽 MOSFET 栅氧电场的屏蔽结构"],
    }


def build_demo_runtime(
    *,
    workspace: Workspace,
    source_text: str,
    source_name: str,
    session_root: Path,
) -> tuple[AgentRuntime, str]:
    registry = ToolRegistry()
    services = {"source_text": source_text, "source_id": source_name}
    if workspace is Workspace.MEETING_AUDIT:
        required_quotes = [
            "张三建议不经过 QA，今天直接把版本推到 main。",
            "研发后续尽快修复导出脚本，相关人员负责。",
        ]
        if not all(quote in source_text for quote in required_quotes):
            raise ValueError("deterministic meeting fixture requires the demo meeting text")
        services.update(
            meeting_state={},
            meeting_model=lambda system, prompt: _meeting_result(),
            rule_search=lambda query, top_k: [
                "所有发布必须经过 QA 验证和代码评审。"
            ],
        )
        register_meeting_tools(registry)
        planner = MeetingPlaybookPlanner()
        capability = build_meeting_capability()
        knowledge_version = "deterministic-rules-v1"
    else:
        patents = load_corpus(CORPUS_PATH)
        services.update(
            patent_state={},
            patent_corpus=patents,
            patent_feature_model=lambda system, prompt: _patent_result(source_text),
            patent_keyword_search=keyword_search,
            patent_semantic_search=lambda queries, limit: [
                PatentHit(
                    "SYN-SIC-009",
                    0.9,
                    frozenset({"semantic"}),
                    semantic_rank=1,
                    semantic_locator="claim:C1",
                ),
                PatentHit(
                    "SYN-SIC-001",
                    0.8,
                    frozenset({"semantic"}),
                    semantic_rank=2,
                    semantic_locator="claim:C1",
                ),
            ],
        )
        register_patent_tools(registry)
        planner = PatentPlaybookPlanner()
        capability = build_patent_capability()
        knowledge_version = corpus_version(CORPUS_PATH)
    return (
        AgentRuntime(
            store=SessionStore(session_root),
            registry=registry,
            planner=planner,
            capability=capability,
            services=services,
        ),
        knowledge_version,
    )
