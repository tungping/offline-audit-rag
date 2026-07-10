import os
import time
from collections.abc import Callable
from typing import Any

import pandas as pd

from audit_core.config import OUTPUT, SEMICONDUCTOR_IP_MODE
from audit_core.file_ops import unique_file_path
from audit_core.formatting import (
    _choice,
    _clean_text,
    _truthy,
    markdown_quote_block,
    markdown_table_cell,
    mask_markdown_text,
    mask_output_basename,
)
from audit_core.history import record_audit_history
from audit_core.models import ProcessResult


SEMICONDUCTOR_IP_DISCLAIMER = (
    "本报告仅用于技术情报整理、专利文本理解和IP初筛，不构成法律意见、"
    "侵权判断、专利有效性判断或投资建议。所有结论均需由具备资质的专业人士复核。"
)
SEMICONDUCTOR_CLAIM_COLUMNS = [
    "claim_id",
    "element_id",
    "technical_feature",
    "structure_or_step",
    "function_effect",
    "evidence_quote",
    "possible_variant",
    "confidence",
    "needs_human_review",
]
SEMICONDUCTOR_RISK_COLUMNS = [
    "risk_type",
    "severity",
    "related_claim_or_paragraph",
    "evidence_quote",
    "reason",
    "suggested_follow_up",
    "needs_human_review",
]


def validate_semiconductor_ip_result(result: dict[str, Any]) -> dict[str, Any]:
    data = result if isinstance(result, dict) else {}
    claim_rows = data.get("claim_chart")
    risk_rows = data.get("risk_items")
    route_rows = data.get("technology_routes")
    questions = data.get("follow_up_questions")
    if not isinstance(claim_rows, list):
        claim_rows = []
    if not isinstance(risk_rows, list):
        risk_rows = []
    if not isinstance(route_rows, list):
        route_rows = []
    if not isinstance(questions, list):
        questions = []

    clean_claim_rows = []
    for row in claim_rows:
        source = row if isinstance(row, dict) else {}
        evidence = _clean_text(source.get("evidence_quote"))
        clean_claim_rows.append(
            {
                "claim_id": _clean_text(source.get("claim_id")),
                "element_id": _clean_text(source.get("element_id")),
                "technical_feature": _clean_text(source.get("technical_feature")),
                "structure_or_step": _clean_text(source.get("structure_or_step")),
                "function_effect": _clean_text(source.get("function_effect")),
                "evidence_quote": evidence,
                "possible_variant": _clean_text(source.get("possible_variant")),
                "confidence": _choice(
                    source.get("confidence"), {"High", "Medium", "Low"}, "Medium"
                ),
                "needs_human_review": _truthy(
                    source.get("needs_human_review")
                )
                or not evidence,
            }
        )

    clean_risk_rows = []
    for row in risk_rows:
        source = row if isinstance(row, dict) else {}
        evidence = _clean_text(source.get("evidence_quote"))
        clean_risk_rows.append(
            {
                "risk_type": _clean_text(source.get("risk_type")),
                "severity": _choice(
                    source.get("severity"), {"High", "Medium", "Low"}, "Medium"
                ),
                "related_claim_or_paragraph": _clean_text(
                    source.get("related_claim_or_paragraph")
                ),
                "evidence_quote": evidence,
                "reason": _clean_text(source.get("reason")),
                "suggested_follow_up": _clean_text(
                    source.get("suggested_follow_up")
                ),
                "needs_human_review": _truthy(
                    source.get("needs_human_review")
                )
                or not evidence,
            }
        )

    clean_routes = []
    for row in route_rows:
        source = row if isinstance(row, dict) else {}
        clean_routes.append(
            {
                "route_name": _clean_text(source.get("route_name")),
                "description": _clean_text(source.get("description")),
                "supporting_evidence": _clean_text(
                    source.get("supporting_evidence")
                ),
                "related_players_or_products": _clean_text(
                    source.get("related_players_or_products")
                ),
            }
        )

    return {
        "technical_topic": _clean_text(data.get("technical_topic")),
        "material_type": _clean_text(data.get("material_type")),
        "summary": _clean_text(data.get("summary")),
        "claim_chart": clean_claim_rows,
        "risk_items": clean_risk_rows,
        "technology_routes": clean_routes,
        "follow_up_questions": [
            _clean_text(item) for item in questions if _clean_text(item)
        ],
        "disclaimer": _clean_text(data.get("disclaimer"))
        or SEMICONDUCTOR_IP_DISCLAIMER,
    }


def build_semiconductor_ip_system_prompt(retrieved_docs: list[str]) -> str:
    rules_context = "\n\n".join(
        f"【规则 {index + 1}】:\n{document}"
        for index, document in enumerate(retrieved_docs)
    )
    return f"""你是半导体知识产权与技术情报分析助手。

基于用户输入文本和检索到的规则，完成半导体专利/IP技术情报初筛。

检索规则：
{rules_context}

严格限制：
1. 只能基于输入文本和检索规则进行分析。
2. 不得编造不存在的技术特征、公司、专利号或法律结论。
3. 不得输出确定侵权、确定不侵权、专利有效或专利无效结论。
4. 每个关键判断尽量给出原文证据片段。
5. 如果证据不足，必须标记 needs_human_review = true。
6. 输出必须是可解析 JSON，不要输出 Markdown。
7. 为了本地模型快速完成，claim_chart 最多 6 条，risk_items 最多 5 条，technology_routes 最多 3 条，follow_up_questions 最多 5 条。

请输出一个 JSON object，字段如下：
{{
  "technical_topic": "string",
  "material_type": "string",
  "summary": "string",
  "claim_chart": [
    {{
      "claim_id": "string",
      "element_id": "string",
      "technical_feature": "string",
      "structure_or_step": "string",
      "function_effect": "string",
      "evidence_quote": "string",
      "possible_variant": "string",
      "confidence": "High/Medium/Low",
      "needs_human_review": true
    }}
  ],
  "risk_items": [
    {{
      "risk_type": "string",
      "severity": "High/Medium/Low",
      "related_claim_or_paragraph": "string",
      "evidence_quote": "string",
      "reason": "string",
      "suggested_follow_up": "string",
      "needs_human_review": true
    }}
  ],
  "technology_routes": [
    {{
      "route_name": "string",
      "description": "string",
      "supporting_evidence": "string",
      "related_players_or_products": "string"
    }}
  ],
  "follow_up_questions": ["string"],
  "disclaimer": "{SEMICONDUCTOR_IP_DISCLAIMER}"
}}"""


def _write_csv_rows(
    rows: list[dict[str, Any]], output_path: str, fieldnames: list[str]
) -> None:
    frame = pd.DataFrame(rows)
    if frame.empty:
        frame = pd.DataFrame(columns=fieldnames)
    else:
        frame = frame.reindex(columns=fieldnames)
    frame.to_csv(output_path, index=False, encoding="utf-8-sig")


def render_semiconductor_ip_report(
    source_file: str,
    audit_time: str,
    content: str,
    retrieved_docs: list[str],
    data: dict[str, Any],
    claim_df: pd.DataFrame,
    risk_df: pd.DataFrame,
) -> str:
    lines = [
        "# 半导体专利与技术情报分析报告",
        "",
        "## 一、输入材料概况",
        "",
        f"- **被处理文件**: `{mask_markdown_text(source_file)}`",
        f"- **分析时间**: `{audit_time}`",
        f"- **分析模式**: `{SEMICONDUCTOR_IP_MODE}`",
        f"- **技术主题**: {mask_markdown_text(data['technical_topic'])}",
        f"- **材料类型**: {mask_markdown_text(data['material_type'])}",
        "",
        "## 二、摘要",
        "",
        mask_markdown_text(data["summary"]) or "未返回摘要。",
        "",
        "## 三、RAG 规则依据",
        "",
    ]
    if retrieved_docs:
        for index, document in enumerate(retrieved_docs, 1):
            lines.append(f"> **参考规则 {index}**:")
            lines.append(markdown_quote_block(mask_markdown_text(document)))
            lines.append("")
    else:
        lines.extend(["> 未命中半导体IP规则，结果需人工复核。", ""])

    lines.extend(
        [
            "## 四、核心权利要求 / 技术特征拆解",
            "",
            "| Claim ID | Element ID | 技术特征 | 结构/步骤 | 功能效果 | 证据 | 置信度 | 人工复核 |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
    )
    for _, row in claim_df.iterrows():
        lines.append(
            f"| {markdown_table_cell(row.get('claim_id'))} | "
            f"{markdown_table_cell(row.get('element_id'))} | "
            f"{markdown_table_cell(mask_markdown_text(row.get('technical_feature')))} | "
            f"{markdown_table_cell(mask_markdown_text(row.get('structure_or_step')))} | "
            f"{markdown_table_cell(mask_markdown_text(row.get('function_effect')))} | "
            f"{markdown_table_cell(mask_markdown_text(row.get('evidence_quote')))} | "
            f"{markdown_table_cell(row.get('confidence'))} | "
            f"{'是' if _truthy(row.get('needs_human_review')) else '否'} |"
        )

    lines.extend(
        [
            "",
            "## 五、技术/IP风险项",
            "",
            "| 风险类型 | 严重级别 | 相关位置 | 证据 | 原因 | 后续建议 | 人工复核 |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
    )
    for _, row in risk_df.iterrows():
        lines.append(
            f"| {markdown_table_cell(mask_markdown_text(row.get('risk_type')))} | "
            f"{markdown_table_cell(row.get('severity'))} | "
            f"{markdown_table_cell(mask_markdown_text(row.get('related_claim_or_paragraph')))} | "
            f"{markdown_table_cell(mask_markdown_text(row.get('evidence_quote')))} | "
            f"{markdown_table_cell(mask_markdown_text(row.get('reason')))} | "
            f"{markdown_table_cell(mask_markdown_text(row.get('suggested_follow_up')))} | "
            f"{'是' if _truthy(row.get('needs_human_review')) else '否'} |"
        )

    lines.extend(["", "## 六、相关技术路线", ""])
    if data["technology_routes"]:
        for route in data["technology_routes"]:
            lines.append(
                f"- **{mask_markdown_text(route['route_name'])}**: "
                f"{mask_markdown_text(route['description'])}"
            )
            if route["supporting_evidence"]:
                lines.append(
                    f"  - 证据: {mask_markdown_text(route['supporting_evidence'])}"
                )
    else:
        lines.append("未提取到明确技术路线。")

    lines.extend(["", "## 七、后续检索建议", ""])
    if data["follow_up_questions"]:
        lines.extend(f"- {mask_markdown_text(item)}" for item in data["follow_up_questions"])
    else:
        lines.append("- 人工复核 claim chart 与风险项证据是否充分。")

    excerpt = mask_markdown_text(
        content[:500] + ("..." if len(content) > 500 else "")
    )
    lines.extend(
        [
            "",
            "## 八、输入原文片段",
            "",
            "```text",
            excerpt,
            "```",
            "",
            "## 九、免责声明",
            "",
            data["disclaimer"],
            "",
        ]
    )
    return "\n".join(lines)


def write_semiconductor_ip_outputs(
    source_file: str,
    content: str,
    retrieved_docs: list[str],
    data: dict[str, Any],
    *,
    output_dir: str = OUTPUT,
    history_recorder: Callable[..., None] = record_audit_history,
) -> ProcessResult:
    audit_time = time.strftime("%Y-%m-%d %H:%M:%S")
    base_name = mask_output_basename(os.path.splitext(source_file)[0])
    time_suffix = time.strftime("%Y-%m-%d_%H_%M")
    claim_csv_path = unique_file_path(
        os.path.join(output_dir, f"{base_name}_{time_suffix}_claim_chart.csv")
    )
    risk_csv_path = unique_file_path(
        os.path.join(output_dir, f"{base_name}_{time_suffix}_ip_risk_items.csv")
    )
    report_path = unique_file_path(
        os.path.join(output_dir, f"{base_name}_{time_suffix}_ip_analysis_report.md")
    )
    _write_csv_rows(data["claim_chart"], claim_csv_path, SEMICONDUCTOR_CLAIM_COLUMNS)
    _write_csv_rows(data["risk_items"], risk_csv_path, SEMICONDUCTOR_RISK_COLUMNS)
    claim_df = pd.read_csv(claim_csv_path)
    risk_df = pd.read_csv(risk_csv_path)
    report = render_semiconductor_ip_report(
        source_file,
        audit_time,
        content,
        retrieved_docs,
        data,
        claim_df,
        risk_df,
    )
    with open(report_path, "w", encoding="utf-8") as report_file:
        report_file.write(report)
    history_recorder(
        source_file=source_file,
        audit_time=audit_time,
        task_output_df=claim_df,
        risk_output_df=risk_df,
        tasks_csv_path=claim_csv_path,
        risk_csv_path=risk_csv_path,
        report_path=report_path,
        mode=SEMICONDUCTOR_IP_MODE,
        output_dir=output_dir,
    )
    return ProcessResult(
        success=True,
        tasks_csv_path=claim_csv_path,
        risk_csv_path=risk_csv_path,
        report_path=report_path,
        mode=SEMICONDUCTOR_IP_MODE,
    )
