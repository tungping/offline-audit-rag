import datetime
import os
import time
from collections.abc import Callable
from typing import Any

import pandas as pd

import audit_rules

from .config import COMPLIANCE_MODE, RELEVANCE_THRESHOLD
from .file_ops import unique_file_path
from .formatting import (
    markdown_quote_block,
    markdown_table_cell,
    mask_dataframe_text_columns,
    mask_markdown_text,
    mask_output_basename,
)
from .models import ProcessResult


def write_meeting_outputs(
    *,
    source_file: str,
    content: str,
    retrieved_docs: list[str],
    data: dict[str, Any],
    output_dir: str,
    history_recorder: Callable[..., None],
) -> ProcessResult:
    if not isinstance(data.get("tasks"), list):
        data["tasks"] = []
    if not isinstance(data.get("sensitive_entities"), list):
        data["sensitive_entities"] = []
    data.setdefault("compliance_risk", "未知")
    data.setdefault("audit_summary", "模型未返回审计总结")
    data.setdefault("model_confidence", "High")
    data.setdefault("uncertainty_reason", "")

    frame = pd.DataFrame(data["tasks"])
    for column, default in {
        "task_name": "未知任务",
        "owner": "Unassigned",
        "priority": "Medium",
    }.items():
        if column not in frame.columns:
            frame[column] = default
    frame = frame.drop_duplicates().reset_index(drop=True)
    frame["owner"] = frame["owner"].fillna("Unassigned").replace({"": "Unassigned"})
    frame["task_name"] = frame["task_name"].fillna("未知任务").replace({"": "未知任务"})
    frame["priority"] = frame["priority"].fillna("Medium").replace({"": "Medium"})
    audit_time = time.strftime("%Y-%m-%d %H:%M:%S")
    frame["audit_time"] = audit_time
    frame["source_file"] = source_file

    today = datetime.date.today()
    due_dates = []
    for priority in frame["priority"]:
        normalized = str(priority).lower()
        days = 3 if "high" in normalized else 7 if "low" in normalized else 5
        due_dates.append((today + datetime.timedelta(days=days)).isoformat())
    frame["due_date"] = due_dates
    data["tasks"] = frame[["task_name", "owner", "priority"]].to_dict("records")

    risk_items = audit_rules.build_risk_items(
        text=content,
        tasks=data["tasks"],
        source_file=source_file,
        sensitive_entities=data.get("sensitive_entities"),
        model_confidence=data.get("model_confidence", "High"),
        uncertainty_reason=data.get("uncertainty_reason", ""),
    )
    risk_columns = [
        "risk_type",
        "severity",
        "evidence_masked",
        "recommendation",
        "manual_review_required",
        "source_file",
        "audit_time",
    ]
    risk_frame = pd.DataFrame(risk_items)
    if risk_frame.empty:
        risk_frame = pd.DataFrame(columns=risk_columns)
    else:
        risk_frame["audit_time"] = audit_time
        risk_frame = risk_frame.reindex(columns=risk_columns)

    task_output = mask_dataframe_text_columns(frame)
    risk_output = mask_dataframe_text_columns(risk_frame)
    base_name = mask_output_basename(os.path.splitext(source_file)[0])
    time_suffix = time.strftime("%Y-%m-%d_%H_%M")
    tasks_path = unique_file_path(
        os.path.join(output_dir, f"{base_name}_{time_suffix}_tasks.csv")
    )
    risks_path = unique_file_path(
        os.path.join(output_dir, f"{base_name}_{time_suffix}_risk_items.csv")
    )
    report_path = unique_file_path(
        os.path.join(output_dir, f"{base_name}_{time_suffix}_audit_report.md")
    )
    task_output.to_csv(tasks_path, index=False, encoding="utf-8-sig")
    risk_output.to_csv(risks_path, index=False, encoding="utf-8-sig")

    report = _render_meeting_report(
        source_file=source_file,
        audit_time=audit_time,
        content=content,
        retrieved_docs=retrieved_docs,
        data=data,
        task_output=task_output,
        risk_output=risk_output,
    )
    with open(report_path, "w", encoding="utf-8") as report_file:
        report_file.write(report)

    history_recorder(
        source_file=source_file,
        audit_time=audit_time,
        task_output_df=task_output,
        risk_output_df=risk_output,
        tasks_csv_path=tasks_path,
        risk_csv_path=risks_path,
        report_path=report_path,
        mode=COMPLIANCE_MODE,
        output_dir=output_dir,
    )
    return ProcessResult(
        success=True,
        tasks_csv_path=tasks_path,
        risk_csv_path=risks_path,
        report_path=report_path,
        mode=COMPLIANCE_MODE,
    )


def _render_meeting_report(
    *,
    source_file: str,
    audit_time: str,
    content: str,
    retrieved_docs: list[str],
    data: dict[str, Any],
    task_output: pd.DataFrame,
    risk_output: pd.DataFrame,
) -> str:
    report = f"""# 自动化合规审计与任务指派报告

## 一、基础审计信息
- **被处理文件**: `{mask_markdown_text(source_file)}`
- **审计结束时间**: `{audit_time}`
- **合规风险评估**: **{mask_markdown_text(data["compliance_risk"])}**
- **事件审计总结**: *{mask_markdown_text(data["audit_summary"])}*

## 二、RAG 语义匹配合规基准条款
"""
    if retrieved_docs:
        report += f"在本次审计中，语义数据库成功为您提取了最相近的 {len(retrieved_docs)} 条合规基线规范：\n"
        for index, document in enumerate(retrieved_docs):
            report += (
                f"\n> **参考规范 {index + 1}**:\n"
                f"{markdown_quote_block(mask_markdown_text(document))}\n"
            )
    else:
        report += (
            f"> ⚠️ **警告**：RAG 语义检索未命中任何合规条款（当前阈值 "
            f"`RELEVANCE_THRESHOLD={RELEVANCE_THRESHOLD}`）。\n"
            "> 本次审计在**无合规参考基准**的情况下完成，结论仅供参考。\n"
            "> 建议适当调高 `.env` 中的 `RELEVANCE_THRESHOLD` 或补充合规手册内容。\n"
        )

    report += """
## 三、提取指派的任务看板
根据对会议内容的解析，自动生成的结构化处理任务如下：

| 序号 | 任务名称 | 负责人 | 优先级 | 截止日期 | 审计生成时间 |
| :--- | :--- | :--- | :--- | :--- | :--- |
"""
    for index, (_, row) in enumerate(task_output.iterrows(), 1):
        report += (
            f"| {index} | {markdown_table_cell(mask_markdown_text(row.get('task_name')))} | "
            f"{markdown_table_cell(mask_markdown_text(row.get('owner')))} | "
            f"{markdown_table_cell(mask_markdown_text(row.get('priority')))} | "
            f"{markdown_table_cell(row.get('due_date'))} | "
            f"{markdown_table_cell(row.get('audit_time'))} |\n"
        )

    report += """
## 四、数据合规与流程治理风险
"""
    if risk_output.empty:
        report += "\n> 未检测到确定性数据治理风险项。\n"
    else:
        report += """
| 序号 | 风险类型 | 等级 | 证据 | 整改建议 | 人工复核 |
| :--- | :--- | :--- | :--- | :--- | :--- |
"""
        for index, (_, row) in enumerate(risk_output.iterrows(), 1):
            manual_review = (
                "是"
                if str(row.get("manual_review_required", "")).lower()
                in ("true", "1", "yes")
                else "否"
            )
            report += (
                f"| {index} | {markdown_table_cell(mask_markdown_text(row.get('risk_type')))} | "
                f"{markdown_table_cell(mask_markdown_text(row.get('severity')))} | "
                f"{markdown_table_cell(mask_markdown_text(row.get('evidence_masked')))} | "
                f"{markdown_table_cell(mask_markdown_text(row.get('recommendation')))} | "
                f"{manual_review} |\n"
            )

    excerpt = mask_markdown_text(
        content[:500] + ("..." if len(content) > 500 else "")
    )
    report += f"""
## 五、会议原始文本摘要
以下为本次审计的原始输入片段（截取前 500 字）：

```text
{excerpt}
```
"""
    return report
