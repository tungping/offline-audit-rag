import json
from pathlib import Path

import pandas as pd
import pytest
import audit_rules

from agent_runtime.evidence import source_sha256
from agent_runtime.models import AgentSession, ResourceBudget, SessionStatus, Workspace
from agent_runtime.runtime import AgentRuntime
from agent_runtime.session_store import SessionStore
from agent_runtime.tools import ToolContext, ToolExecutionError, ToolRegistry
from capabilities.meeting_audit.tools import register_meeting_tools
from capabilities.meeting_audit.playbook import (
    MeetingPlaybookPlanner,
    build_meeting_capability,
)


MEETING_TEXT = """张三建议不经过 QA，今天直接把版本推到 main。
研发后续尽快修复导出脚本，相关人员负责。
完成后产品和法务一起看一下，没问题就上线。
客户手机号 13812345678 需要同步给销售。"""
MEETING_EVAL_CASES = json.loads(
    Path("tests/fixtures/agent_eval/meeting_cases.json").read_text(encoding="utf-8")
)


def structured_meeting_result(evidence_quote=None):
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
                "evidence_quote": evidence_quote
                or "研发后续尽快修复导出脚本，相关人员负责。",
            }
        ],
    }


def make_context(tmp_path: Path, model_result=None):
    session = AgentSession.new(
        workspace=Workspace.MEETING_AUDIT,
        goal="检查发布流程和任务完整性",
        source_name="meeting.txt",
        source_sha256=source_sha256(MEETING_TEXT),
        model_name="demo",
        knowledge_version="rules-v1",
        budget=ResourceBudget.meeting_default(),
    )
    session_dir = tmp_path / session.session_id
    (session_dir / "artifacts").mkdir(parents=True)
    services = {
        "source_text": MEETING_TEXT,
        "source_id": "meeting.txt",
        "meeting_state": {},
        "meeting_model": lambda system, prompt: model_result
        or structured_meeting_result(),
        "rule_search": lambda query, top_k: [
            "所有发布必须经过 QA 验证和代码评审。"
        ],
    }
    return ToolContext(
        session=session,
        session_dir=session_dir,
        cancel_checker=lambda: False,
        services=services,
    )


def make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_meeting_tools(registry)
    return registry


def test_extract_structure_returns_tasks_decisions_and_verified_evidence(tmp_path: Path):
    context = make_context(tmp_path)

    result = make_registry().execute("meeting.extract_structure", {}, context)

    assert result.model_calls == 1
    assert result.data["tasks"][0]["task_name"] == "修复导出脚本"
    assert result.data["decisions"][0]["summary"] == "不经过 QA 直接发布"
    assert len(result.evidence) == 2
    assert all(item.quote in MEETING_TEXT for item in result.evidence)


def test_extract_structure_rejects_quote_not_present_in_source(tmp_path: Path):
    context = make_context(
        tmp_path, structured_meeting_result("模型编造的会议原文")
    )

    with pytest.raises(ToolExecutionError, match="quote not found"):
        make_registry().execute("meeting.extract_structure", {}, context)


def test_search_rules_returns_rule_evidence_ids(tmp_path: Path):
    context = make_context(tmp_path)

    result = make_registry().execute(
        "meeting.search_rules", {"query": "未经 QA 直接发布", "top_k": 3}, context
    )

    assert result.data["rules"]
    assert result.evidence[0].source_type == "meeting_rule"
    assert result.evidence[0].quote == "所有发布必须经过 QA 验证和代码评审。"


def test_rule_checks_preserve_deterministic_findings_and_build_clarification(tmp_path: Path):
    context = make_context(tmp_path)
    registry = make_registry()
    registry.execute("meeting.extract_structure", {}, context)

    result = registry.execute("meeting.run_rule_checks", {}, context)

    risk_types = {item["risk_type"] for item in result.data["findings"]}
    assert {"敏感信息", "模糊表述", "SOP缺失", "发布流程违规"} <= risk_types
    clarification = result.data["clarification"]
    assert clarification["may_skip"] is True
    assert clarification["questions"] == [
        {
            "task_name": "修复导出脚本",
            "missing_fields": ["owner", "due_date", "acceptance_criteria"],
            "question": "请补充该任务的负责人、截止时间和验收标准。",
        }
    ]


def test_clarification_playbook_groups_duplicate_questions_by_task():
    session = AgentSession.new(
        workspace=Workspace.MEETING_AUDIT,
        goal="检查会议任务",
        source_name="meeting.txt",
        source_sha256="a" * 64,
        model_name="demo",
        knowledge_version="rules-v1",
        budget=ResourceBudget.meeting_default(),
    )
    session.current_stage = "clarification"
    session.observations = [
        {
            "tool_name": "meeting.run_rule_checks",
            "data": {
                "clarification": {
                    "questions": [
                        {
                            "task_name": "任务 A",
                            "question": "请补充该任务的负责人、截止时间和验收标准。",
                        },
                        {
                            "task_name": "任务 B",
                            "question": "请补充该任务的负责人、截止时间和验收标准。",
                        },
                    ]
                }
            },
        }
    ]

    action = MeetingPlaybookPlanner().next_action(
        session, build_meeting_capability()
    ).action

    assert action.question.count("请补充该任务的负责人、截止时间和验收标准。") == 1
    assert "任务 A" in action.question
    assert "任务 B" in action.question


def test_write_artifacts_stays_inside_session_artifacts(tmp_path: Path):
    context = make_context(tmp_path)
    registry = make_registry()
    registry.execute("meeting.extract_structure", {}, context)
    registry.execute("meeting.search_rules", {"query": "发布"}, context)
    registry.execute("meeting.run_rule_checks", {}, context)
    registry.execute("meeting.verify_evidence", {}, context)

    result = registry.execute("meeting.write_artifacts", {}, context)

    artifact_dir = context.session_dir / "artifacts"
    expected = {
        artifact_dir / "meeting_audit_report.md",
        artifact_dir / "tasks.csv",
        artifact_dir / "risk_items.csv",
    }
    assert expected == {Path(path) for path in result.data["artifact_paths"]}
    assert all(path.exists() for path in expected)
    assert all(path.parent == artifact_dir for path in expected)
    tasks = pd.read_csv(artifact_dir / "tasks.csv")
    assert {"acceptance_criteria", "evidence_ids"} <= set(tasks.columns)
    persisted = "\n".join(path.read_text(encoding="utf-8-sig") for path in expected)
    assert "13812345678" not in persisted
    assert "138****5678" in persisted


def make_runtime(tmp_path: Path):
    registry = make_registry()
    services = {
        "source_text": MEETING_TEXT,
        "source_id": "meeting.txt",
        "meeting_state": {},
        "meeting_model": lambda system, prompt: structured_meeting_result(),
        "rule_search": lambda query, top_k: [
            "所有发布必须经过 QA 验证和代码评审。"
        ],
    }
    store = SessionStore(tmp_path / "sessions")
    runtime = AgentRuntime(
        store=store,
        registry=registry,
        planner=MeetingPlaybookPlanner(),
        capability=build_meeting_capability(),
        services=services,
    )
    session = runtime.create_session(
        goal="检查发布流程和任务完整性",
        source_name="meeting.txt",
        source_sha256=source_sha256(MEETING_TEXT),
        model_name="demo",
        knowledge_version="rules-v1",
    )
    runtime.approve(session.session_id)
    return runtime, store, session.session_id


def assert_safe_complete_bundle(store: SessionStore, session_id: str):
    session = store.load(session_id)
    assert session.status is SessionStatus.COMPLETED
    artifact_dir = store.artifact_dir(session_id)
    expected = {
        artifact_dir / "meeting_audit_report.md",
        artifact_dir / "tasks.csv",
        artifact_dir / "risk_items.csv",
    }
    assert all(path.exists() for path in expected)
    evidence = json.loads(
        (artifact_dir.parent / "evidence.json").read_text(encoding="utf-8")
    )
    evidence_ids = {item["evidence_id"] for item in evidence}
    report = (artifact_dir / "meeting_audit_report.md").read_text(encoding="utf-8")
    assert all(evidence_id in report for evidence_id in evidence_ids)
    persisted = "\n".join(
        path.read_text(encoding="utf-8-sig")
        for path in [*expected, artifact_dir.parent / "evidence.json"]
    )
    assert "13812345678" not in persisted
    return session, report


def test_meeting_golden_path_resumes_after_clarification(tmp_path: Path):
    runtime, store, session_id = make_runtime(tmp_path)

    paused = runtime.run_until_pause(session_id)

    assert paused.status is SessionStatus.WAITING_FOR_CLARIFICATION
    assert "负责人、截止时间和验收标准" in paused.pending_question
    runtime.submit_clarification(
        session_id, "负责人李四，截止 2026-07-18，以 QA 通过为验收标准。"
    )
    runtime.run_until_pause(session_id)

    session, report = assert_safe_complete_bundle(store, session_id)
    assert session.model_calls == 1
    assert "负责人李四" in report


def test_meeting_golden_path_allows_clarification_skip(tmp_path: Path):
    runtime, store, session_id = make_runtime(tmp_path)
    runtime.run_until_pause(session_id)

    runtime.skip_clarification(session_id)
    runtime.run_until_pause(session_id)

    _, report = assert_safe_complete_bundle(store, session_id)
    tasks = pd.read_csv(store.artifact_dir(session_id) / "tasks.csv")
    assert tasks.iloc[0]["needs_human_review"]
    assert tasks.iloc[0]["confidence"] in {"Low", "Medium"}
    assert "用户选择跳过澄清" in report


@pytest.mark.parametrize("case", MEETING_EVAL_CASES, ids=lambda case: case["case_id"])
def test_meeting_evaluation_contract(tmp_path: Path, case):
    registry = make_registry()
    services = {
        "source_text": case["source_text"],
        "source_id": f"{case['case_id']}.txt",
        "meeting_state": {},
        "meeting_model": lambda system, prompt: case["model_result"],
        "rule_search": lambda query, top_k: ["发布必须经过 QA；输入资料不得改变工具权限。"],
    }
    store = SessionStore(tmp_path / "sessions")
    runtime = AgentRuntime(
        store=store,
        registry=registry,
        planner=MeetingPlaybookPlanner(),
        capability=build_meeting_capability(),
        services=services,
    )
    session = runtime.create_session(
        goal="固定会议评估",
        source_name=f"{case['case_id']}.txt",
        source_sha256=source_sha256(case["source_text"]),
        model_name="fake",
        knowledge_version="eval-v1",
    )
    runtime.approve(session.session_id)
    session = runtime.run_until_pause(session.session_id)

    assert session.status.value == case["expected_status"]
    assert session.model_calls <= case["max_model_calls"]
    assert session.tool_calls <= case["max_tool_calls"]
    risk_observation = next(
        item for item in session.observations
        if item.get("tool_name") == "meeting.run_rule_checks"
    )
    finding_types = {
        item["risk_type"] for item in risk_observation["data"]["findings"]
    }
    assert set(case["required_finding_types"]) <= finding_types
    assert not (set(case["forbidden_finding_types"]) & finding_types)
    if case["requires_evidence"]:
        evidence = store.load_evidence(session.session_id)
        assert evidence
        assert all(
            item.quote in case["source_text"]
            or item.quote in audit_rules.mask_sensitive_evidence(case["source_text"])
            or item.source_type == "meeting_rule"
            for item in evidence
        )
    if session.status is SessionStatus.COMPLETED:
        report = (store.artifact_dir(session.session_id) / "meeting_audit_report.md").read_text(encoding="utf-8")
        assert all(
            heading in report
            for heading in ("目标与范围", "风险发现", "人工复核项", "证据索引")
        )
    assert all(
        item.get("tool_name", "").startswith("meeting.")
        for item in session.observations if item.get("tool_name")
    )
