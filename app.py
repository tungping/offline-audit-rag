import argparse
import asyncio
import datetime
import json
import logging
import os
import queue
import re
import shutil
import sys
import threading
import time
from typing import Any, cast

import chromadb
import ollama
import pandas as pd
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import audit_rules
from audit_core.config import (
    ARCHIVE,
    AUDIT_HISTORY_FILENAME,
    AUDIT_INPUT_TOKEN_LIMIT,
    AUDIT_MODEL,
    BASE_DIR,
    COMPLIANCE_MODE,
    CONFIG_DIR,
    EMBEDDING_CONCURRENCY,
    EMBEDDING_MAX_RETRIES,
    EMBED_MODEL,
    FAILED,
    INBOX,
    OUTPUT,
    POLL_INTERVAL,
    RELEVANCE_THRESHOLD,
    SEMICONDUCTOR_IP_CONFIG_DIR,
    SEMICONDUCTOR_IP_INPUT_TOKEN_LIMIT,
    SEMICONDUCTOR_IP_MODE,
    SEMICONDUCTOR_IP_NUM_PREDICT,
    SUPPORTED_AUDIT_MODES,
    VECTOR_STORE_DIR,
    collection_name_for_mode,
    generation_options_for_mode,
    input_token_limit_for_mode,
    normalize_audit_mode,
    rules_dir_for_mode,
)
from audit_core.file_ops import safe_move, unique_file_path
from audit_core.formatting import (
    _choice,
    _clean_text,
    _truthy,
    extract_json_object,
    markdown_quote_block,
    markdown_table_cell,
    mask_dataframe_text_columns,
    mask_markdown_text,
    mask_output_basename,
)
from audit_core.models import ProcessResult
from audit_core.text_processing import (
    bound_audit_prompt_content,
    count_tokens,
    recursive_split_text,
)

# ──────────────────────────────────────────────
# 环境初始化与日志
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# 事件循环与队列
file_queue = queue.Queue()
_queued_paths: set[str] = set()
_queue_lock = threading.Lock()

# 严密的 SYSTEM PROMPT 模板
SYSTEM_PROMPT_TEMPLATE = """You are an expert PMO (Project Management Office) Compliance Auditor and Technical Operations Analyst. Your job is to analyze messy, unformatted technical meeting notes or logs, perform a strict compliance check against standard software engineering workflows using the provided compliance standards, and extract actionable tasks.

### 核心合规参考标准（必须作为审计违规判断与任务抽取的唯一基准）：
{compliance_context}

### 审计与任务提取规则：
STEP 1: Analyze the input text for any project compliance violations based on the reference standard, such as:
- Deploying directly to production without testing, QA approval, or Peer Code Review.
- Unauthorized scope changes (scope creep) without updating documentation.
- Lack of clear ownership or chaotic task assignments.
- Bypassing standard Git workflows (e.g., pushing directly to master/main).

STEP 2: Handle technical typos gracefully. Understand what technology the team is referring to (e.g., "Pyton" -> Python, "Kubernets" -> Kubernetes, "Golag" -> Golang, "Doker" -> Docker, "Githb" -> GitHub) and normalize them in your analysis.

STEP 3: Extract clear, actionable tasks. If a task lacks an explicit owner, assign "Unassigned". Estimate priority (High, Medium, Low) based on the urgency discussed.

STEP 4: Identify and extract any sensitive entities from the text, specifically:
- "客户名称" (customer names, e.g., '张女士', '王先生')
- "员工信息" (employee names, titles, departments, or employee IDs, e.g., '研发小李', '测试小陈')

STEP 5: Assess your own confidence level in your auditing and task extraction. If the input notes are extremely vague, lack context, or have conflicting guidance, output a confidence of "Medium" or "Low" and specify the reason. Otherwise, output "High".

*Please keep your internal thinking process (thinking) extremely concise, brief, and to the point. Do not write excessively long analysis in the thinking phase.*

OUTPUT FORMAT:
You MUST reply strictly in the following JSON format. Do not include any markdown formatting like "```json", do not include any preamble, and do not include any post-summary. Your entire response must be a single, parsable JSON object:

{{
  "compliance_risk": "高/中/低（给出清晰的中文违规定性与具体的合规条款判定说明）",
  "audit_summary": "一句话中文总结本次技术事件",
  "model_confidence": "High/Medium/Low",
  "uncertainty_reason": "判定置信度为 Medium 或 Low 的具体原因说明（若置信度为 High 请填空字符串）",
  "sensitive_entities": [
    {{
      "entity_type": "客户名称/员工信息",
      "entity_value": "识别到的具体名称或敏感文本"
    }}
  ],
  "tasks": [
    {{
      "task_name": "标准化后的中文待办任务描述 (在此处纠正技术名词拼写错误)",
      "owner": "负责人姓名，若无明确负责人则为 'Unassigned'",
      "priority": "High/Medium/Low"
    }}
  ]
}}"""


async def _fetch_embeddings_async(documents: list[str]) -> list[list[float]]:
    """
    使用有限并发批量获取文档向量。
    """
    client = ollama.AsyncClient()
    semaphore = asyncio.Semaphore(max(1, EMBEDDING_CONCURRENCY))

    async def embed_one(doc: str) -> list[float]:
        async with semaphore:
            for attempt in range(EMBEDDING_MAX_RETRIES):
                try:
                    res = await client.embeddings(model=EMBED_MODEL, prompt=doc)
                    return cast(list[float], res["embedding"])
                except Exception:
                    if attempt == EMBEDDING_MAX_RETRIES - 1:
                        raise
                    await asyncio.sleep(1 + attempt)
            raise RuntimeError("Unreachable")

    results = await asyncio.gather(*(embed_one(doc) for doc in documents))
    return results


def initialize_knowledge_base(mode: str = COMPLIANCE_MODE):
    """
    检查并初始化 ChromaDB 本地向量库。如果库中无数据，读取对应模式规则切片后写入。
    """
    mode = normalize_audit_mode(mode)
    rules_dir = rules_dir_for_mode(mode)
    client = chromadb.PersistentClient(path=VECTOR_STORE_DIR)
    collection = client.get_or_create_collection(name=collection_name_for_mode(mode))

    if collection.count() > 0:
        logging.info("本地向量数据库已检测到 %s 数据，跳过向量库构建。", mode)
        return collection

    logging.info("本地向量数据库为空，开始读取 %s 规则...", mode)

    files = sorted([f for f in os.listdir(rules_dir) if f.endswith(".txt")])
    if not files and mode == COMPLIANCE_MODE:
        default_rule_path = os.path.join(
            rules_dir, "standard_pmo_compliance_rules.txt"
        )
        default_rules = """标准研发及发布合规规范手册（PMO Compliance Standard）：

1. 分支开发管理与代码评审规范：
   - 严禁任何开发人员直接向 master 或 main 等受保护分支推送代码（Bypassing Git workflow）。
   - 所有代码修改必须在专属特征分支（Feature Branch）上进行开发。
   - 开发完成后，必须向受保护分支提交 Merge Request (MR) 或 Pull Request (PR)。
   - 必须有至少一名团队内其他开发人员（Peer/TL）进行 Code Review 代码审查并批准后，方能合并代码。

2. QA 测试与生产发布合规规范：
   - 严禁绕过 QA 测试环节以及团队标准 CI/CD 流水线，进行强制手动生产部署。
   - 所有部署到生产环境的代码包、镜像（Docker Image），必须经过测试环境（Staging/Test）QA 团队验证通过并签署发布报告。
   - 部署必须由专门运维人员或者规范的发布流水线按发布窗口执行，并确保系统配置可被审计追溯。

3. 需求变更管理（Scope Creep）规范：
   - 严禁在未更新 PRD（产品需求文档）、需求规格说明书或者系统架构文档的情况下，私自上线新功能或更改核心业务逻辑。
   - 任何临时需求或重大业务变更必须先由 PM/PMO 评审，并在 Wiki 或文档库中补充规范文档归档后，方能安排开发排期与任务分配。
   - 紧急故障修复期间必须保持职责清晰分工，防止混乱操作。
"""
        with open(default_rule_path, "w", encoding="utf-8") as f_def:
            f_def.write(default_rules)
        files = [os.path.basename(default_rule_path)]
        logging.info(f"已为您自动生成默认合规规范文本: {default_rule_path}")

    if not files and mode == SEMICONDUCTOR_IP_MODE:
        default_rule_path = os.path.join(rules_dir, "sic_power_device_terms.txt")
        default_rules = """SiC功率半导体IP初筛规则：

1. 只基于输入文本和检索规则做技术情报整理，不输出法律意见。
2. 优先拆解权利要求或核心技术特征中的结构、材料、步骤、功能和技术效果。
3. 每个关键判断尽量给出原文证据；证据不足时标记人工复核。
4. 不得输出确定侵权、确定不侵权、专利有效或专利无效结论。
5. SiC MOSFET 分析重点包括 trench gate、drift layer、gate oxide reliability、on-resistance、breakdown voltage、thermal resistance 和 edge termination。
"""
        with open(default_rule_path, "w", encoding="utf-8") as f_def:
            f_def.write(default_rules)
        files = [os.path.basename(default_rule_path)]
        logging.info("已自动生成默认半导体IP规则文本: %s", default_rule_path)

    documents = []
    ids = []
    chunk_counter = 0

    for file in files:
        file_path = os.path.join(rules_dir, file)
        with open(file_path, "r", encoding="utf-8") as f_in:
            text_content = f_in.read()

        chunks = recursive_split_text(text_content, chunk_size=500, chunk_overlap=200)
        for chunk in chunks:
            if chunk.strip():
                documents.append(chunk)
                ids.append(f"chunk_{chunk_counter}")
                chunk_counter += 1

    if not documents:
        logging.warning("未提取到任何有效切片文本。")
        return collection

    logging.info(
        f"已生成 {len(documents)} 个 {mode} 切片，正在并发调用 {EMBED_MODEL} 写入本地 ChromaDB..."
    )

    embeddings: Any = asyncio.run(_fetch_embeddings_async(documents))

    collection.add(ids=ids, documents=documents, embeddings=embeddings)
    logging.info("向量数据库构建成功！数据已持久化。")
    return collection


def retrieve_relevant_context(collection, query_text, top_k=3):
    """
    语义检索网关：将查询日志转为向量并在本地 collection 中检索出最相关的 top_k 条合规基准，
    利用 RELEVANCE_THRESHOLD 进行相关度度量过滤。
    对于长输入，采用分段检索以避免整体向量化导致精度下降。
    """
    # 估算 Token。若输入较短直接检索，若较长进行分片检索
    if count_tokens(query_text) <= 500:
        texts_to_embed = [query_text]
    else:
        # 分片检索并去重聚合
        texts_to_embed = recursive_split_text(query_text, chunk_size=400, chunk_overlap=100)
        if len(texts_to_embed) > 10:
            texts_to_embed = texts_to_embed[:10]

    all_retrieved = []
    seen_docs = set()

    for text in texts_to_embed:
        if not text.strip():
            continue
        try:
            res = ollama.embeddings(model=EMBED_MODEL, prompt=text)
            query_embedding = res["embedding"]
            results = collection.query(
                query_embeddings=[query_embedding],
                n_results=top_k,
                include=["documents", "distances"],
            )

            if results and "documents" in results and results["documents"]:
                docs = results["documents"][0]
                distances = results.get("distances", [[]])[0]
                for doc, dist in zip(docs, distances):
                    if dist < RELEVANCE_THRESHOLD:
                        if doc not in seen_docs:
                            seen_docs.add(doc)
                            all_retrieved.append((doc, dist))
                    else:
                        logging.debug(
                            f"过滤低相关度基准 (distance={dist:.4f} >= threshold={RELEVANCE_THRESHOLD}): {doc[:30]}..."
                        )
        except Exception as e:
            logging.error(f"提取分片 Embedding 检索失败: {e}")

    # 按相似度（距离越小越好）排序，截取前 top_k 个
    all_retrieved.sort(key=lambda x: x[1])
    retrieved_docs = [doc for doc, dist in all_retrieved[:top_k]]

    if not retrieved_docs:
        logging.warning(
            f"RAG 未检索到任何相关合规条款（threshold={RELEVANCE_THRESHOLD}），"
            "审计将在无参考基准的情况下进行。可适当调高 RELEVANCE_THRESHOLD。"
        )
    return retrieved_docs


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
                "confidence": _choice(source.get("confidence"), {"High", "Medium", "Low"}, "Medium"),
                "needs_human_review": _truthy(source.get("needs_human_review")) or not evidence,
            }
        )

    clean_risk_rows = []
    for row in risk_rows:
        source = row if isinstance(row, dict) else {}
        evidence = _clean_text(source.get("evidence_quote"))
        clean_risk_rows.append(
            {
                "risk_type": _clean_text(source.get("risk_type")),
                "severity": _choice(source.get("severity"), {"High", "Medium", "Low"}, "Medium"),
                "related_claim_or_paragraph": _clean_text(source.get("related_claim_or_paragraph")),
                "evidence_quote": evidence,
                "reason": _clean_text(source.get("reason")),
                "suggested_follow_up": _clean_text(source.get("suggested_follow_up")),
                "needs_human_review": _truthy(source.get("needs_human_review")) or not evidence,
            }
        )

    clean_routes = []
    for row in route_rows:
        source = row if isinstance(row, dict) else {}
        clean_routes.append(
            {
                "route_name": _clean_text(source.get("route_name")),
                "description": _clean_text(source.get("description")),
                "supporting_evidence": _clean_text(source.get("supporting_evidence")),
                "related_players_or_products": _clean_text(source.get("related_players_or_products")),
            }
        )

    return {
        "technical_topic": _clean_text(data.get("technical_topic")),
        "material_type": _clean_text(data.get("material_type")),
        "summary": _clean_text(data.get("summary")),
        "claim_chart": clean_claim_rows,
        "risk_items": clean_risk_rows,
        "technology_routes": clean_routes,
        "follow_up_questions": [_clean_text(item) for item in questions if _clean_text(item)],
        "disclaimer": _clean_text(data.get("disclaimer")) or SEMICONDUCTOR_IP_DISCLAIMER,
    }


def build_semiconductor_ip_system_prompt(retrieved_docs: list[str]) -> str:
    rules_context = "\n\n".join(
        [f"【规则 {i + 1}】:\n{doc}" for i, doc in enumerate(retrieved_docs)]
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


def _write_csv_rows(rows: list[dict[str, Any]], output_path: str, fieldnames: list[str]) -> None:
    df = pd.DataFrame(rows)
    if df.empty:
        df = pd.DataFrame(columns=fieldnames)
    else:
        df = df.reindex(columns=fieldnames)
    df.to_csv(output_path, index=False, encoding="utf-8-sig")


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
        for idx, doc in enumerate(retrieved_docs, 1):
            lines.append(f"> **参考规则 {idx}**:")
            lines.append(markdown_quote_block(mask_markdown_text(doc)))
            lines.append("")
    else:
        lines.append("> 未命中半导体IP规则，结果需人工复核。")
        lines.append("")

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
                lines.append(f"  - 证据: {mask_markdown_text(route['supporting_evidence'])}")
    else:
        lines.append("未提取到明确技术路线。")

    lines.extend(["", "## 七、后续检索建议", ""])
    if data["follow_up_questions"]:
        for question in data["follow_up_questions"]:
            lines.append(f"- {mask_markdown_text(question)}")
    else:
        lines.append("- 人工复核 claim chart 与风险项证据是否充分。")

    excerpt = mask_markdown_text(content[:500] + ("..." if len(content) > 500 else ""))
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
) -> ProcessResult:
    audit_time = time.strftime("%Y-%m-%d %H:%M:%S")
    base_name = mask_output_basename(os.path.splitext(source_file)[0])
    time_suffix = time.strftime("%Y-%m-%d_%H_%M")

    claim_csv_path = unique_file_path(
        os.path.join(OUTPUT, f"{base_name}_{time_suffix}_claim_chart.csv")
    )
    risk_csv_path = unique_file_path(
        os.path.join(OUTPUT, f"{base_name}_{time_suffix}_ip_risk_items.csv")
    )
    report_path = unique_file_path(
        os.path.join(OUTPUT, f"{base_name}_{time_suffix}_ip_analysis_report.md")
    )

    _write_csv_rows(data["claim_chart"], claim_csv_path, SEMICONDUCTOR_CLAIM_COLUMNS)
    _write_csv_rows(data["risk_items"], risk_csv_path, SEMICONDUCTOR_RISK_COLUMNS)

    claim_df = pd.read_csv(claim_csv_path)
    risk_df = pd.read_csv(risk_csv_path)
    md_content = render_semiconductor_ip_report(
        source_file=source_file,
        audit_time=audit_time,
        content=content,
        retrieved_docs=retrieved_docs,
        data=data,
        claim_df=claim_df,
        risk_df=risk_df,
    )
    with open(report_path, "w", encoding="utf-8") as report_file:
        report_file.write(md_content)

    record_audit_history(
        source_file=source_file,
        audit_time=audit_time,
        task_output_df=claim_df,
        risk_output_df=risk_df,
        tasks_csv_path=claim_csv_path,
        risk_csv_path=risk_csv_path,
        report_path=report_path,
        mode=SEMICONDUCTOR_IP_MODE,
    )

    return ProcessResult(
        success=True,
        tasks_csv_path=claim_csv_path,
        risk_csv_path=risk_csv_path,
        report_path=report_path,
        mode=SEMICONDUCTOR_IP_MODE,
    )


def record_audit_history(
    source_file: str,
    audit_time: str,
    task_output_df: pd.DataFrame,
    risk_output_df: pd.DataFrame,
    tasks_csv_path: str,
    risk_csv_path: str,
    report_path: str,
    mode: str = COMPLIANCE_MODE,
) -> None:
    history_path = os.path.join(OUTPUT, AUDIT_HISTORY_FILENAME)
    severity_counts = (
        risk_output_df["severity"].value_counts().to_dict()
        if "severity" in risk_output_df.columns
        else {}
    )
    risk_type_counts = (
        risk_output_df["risk_type"].value_counts().to_dict()
        if "risk_type" in risk_output_df.columns
        else {}
    )
    manual_review_count = 0
    review_column = ""
    if "manual_review_required" in risk_output_df.columns:
        review_column = "manual_review_required"
    elif "needs_human_review" in risk_output_df.columns:
        review_column = "needs_human_review"
    if review_column:
        manual_review_count = int(risk_output_df[review_column].map(_truthy).sum())

    entry = {
        "audit_time": audit_time,
        "mode": mode,
        "source_file": mask_markdown_text(source_file),
        "task_count": int(len(task_output_df)),
        "risk_count": int(len(risk_output_df)),
        "high_risk_count": int(severity_counts.get("High", 0)),
        "manual_review_count": manual_review_count,
        "severity_counts": {str(key): int(value) for key, value in severity_counts.items()},
        "risk_type_counts": {str(key): int(value) for key, value in risk_type_counts.items()},
        "tasks_csv_path": os.path.basename(tasks_csv_path),
        "risk_csv_path": os.path.basename(risk_csv_path),
        "report_path": os.path.basename(report_path),
    }

    with open(history_path, "a", encoding="utf-8") as history_file:
        history_file.write(json.dumps(entry, ensure_ascii=False) + "\n")


def process_file_with_result(
    file_path,
    collection,
    progress_prefix="",
    cancel_checker=None,
    mode: str = COMPLIANCE_MODE,
):
    """
    对单个文件执行完整的 RAG 审计流程。
    """
    mode = normalize_audit_mode(mode)
    full_response = ""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 1. 语义检索模式规则
        retrieved_docs = retrieve_relevant_context(collection, content, top_k=3)

        # 2. 组装 SYSTEM PROMPT
        if mode == SEMICONDUCTOR_IP_MODE:
            system_prompt = build_semiconductor_ip_system_prompt(retrieved_docs)
        else:
            compliance_context = "\n\n".join(
                [f"【条款 {i + 1}】:\n{doc}" for i, doc in enumerate(retrieved_docs)]
            )
            system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
                compliance_context=compliance_context
            )

        # 3. 本地大模型推理并流式监听
        logging.info(f"大模型分析中 (模型: {AUDIT_MODEL})，如需强制退出请按 Ctrl+C")
        audit_prompt_content = bound_audit_prompt_content(
            content,
            max_tokens=input_token_limit_for_mode(mode),
        )
        response_stream = ollama.generate(
            model=AUDIT_MODEL,
            prompt=audit_prompt_content,
            system=system_prompt,
            options=generation_options_for_mode(mode),
            stream=True,
        )

        spinner = ["|", "/", "-", "\\"]
        spinner_idx = 0

        if not progress_prefix:
            progress_prefix = f"正在分析: {os.path.basename(file_path)}"

        for chunk in response_stream:
            if cancel_checker is not None and cancel_checker():
                print(f"\r{progress_prefix}... 已取消   \n")
                logging.info(f"工作流【{os.path.basename(file_path)}】已取消。")
                return ProcessResult(success=False, cancelled=True)
            token = chunk.get("response", "")
            full_response += token
            print(f"\r{progress_prefix}... {spinner[spinner_idx]} ", end="", flush=True)
            spinner_idx = (spinner_idx + 1) % len(spinner)

        print(f"\r{progress_prefix}... 完成!   \n")

        # 4. 模型响应过滤与容错 JSON 解析
        data = json.loads(extract_json_object(full_response))
        if mode == SEMICONDUCTOR_IP_MODE:
            source_file = os.path.basename(file_path)
            result = write_semiconductor_ip_outputs(
                source_file=source_file,
                content=content,
                retrieved_docs=retrieved_docs,
                data=validate_semiconductor_ip_result(data),
            )
            logging.info(
                f"工作流【{os.path.basename(file_path)}】处理成功！结果已保存至 output/ 目录。"
            )
            return result

        if not isinstance(data.get("tasks"), list):
            data["tasks"] = []
        if not isinstance(data.get("sensitive_entities"), list):
            data["sensitive_entities"] = []
        data.setdefault("compliance_risk", "未知")
        data.setdefault("audit_summary", "模型未返回审计总结")
        data.setdefault("model_confidence", "High")
        data.setdefault("uncertainty_reason", "")
        source_file = os.path.basename(file_path)

        # 5. 数据清洗与加工 (CSV/Pandas)
        df = pd.DataFrame(data["tasks"])
        for column, default in {
            "task_name": "未知任务",
            "owner": "Unassigned",
            "priority": "Medium",
        }.items():
            if column not in df.columns:
                df[column] = default
        df = df.drop_duplicates().reset_index(
            drop=True
        )  # reset_index 保证 iterrows 序号连续

        df["owner"] = df["owner"].fillna("Unassigned").replace({"": "Unassigned"})
        df["task_name"] = df["task_name"].fillna("未知任务").replace({"": "未知任务"})
        df["priority"] = df["priority"].fillna("Medium").replace({"": "Medium"})
        audit_time = time.strftime("%Y-%m-%d %H:%M:%S")
        df["audit_time"] = audit_time

        # 为 Jira 兼容拓展字段
        df["source_file"] = source_file

        due_dates = []
        today = datetime.date.today()
        for p in df["priority"]:
            p_lower = str(p).lower()
            if "high" in p_lower:
                days = 3
            elif "low" in p_lower:
                days = 7
            else:
                days = 5
            due_dates.append((today + datetime.timedelta(days=days)).isoformat())
        df["due_date"] = due_dates
        data["tasks"] = df[["task_name", "owner", "priority"]].to_dict("records")

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
        risk_df = pd.DataFrame(risk_items)
        if risk_df.empty:
            risk_df = pd.DataFrame(columns=risk_columns)
        else:
            risk_df["audit_time"] = audit_time
            risk_df = risk_df.reindex(columns=risk_columns)
        task_output_df = mask_dataframe_text_columns(df)
        risk_output_df = mask_dataframe_text_columns(risk_df)

        base_name = mask_output_basename(os.path.splitext(os.path.basename(file_path))[0])
        time_suffix = time.strftime("%Y-%m-%d_%H_%M")

        # 保存 CSV 任务指派表
        csv_path = unique_file_path(
            os.path.join(OUTPUT, f"{base_name}_{time_suffix}_tasks.csv")
        )
        task_output_df.to_csv(csv_path, index=False, encoding="utf-8-sig")

        risk_csv_path = unique_file_path(
            os.path.join(OUTPUT, f"{base_name}_{time_suffix}_risk_items.csv")
        )
        risk_output_df.to_csv(risk_csv_path, index=False, encoding="utf-8-sig")

        # 6. 生成排版美观的 Markdown 合规审计报告
        md_content = f"""# 自动化合规审计与任务指派报告

## 一、基础审计信息
- **被处理文件**: `{mask_markdown_text(source_file)}`
- **审计结束时间**: `{audit_time}`
- **合规风险评估**: **{mask_markdown_text(data["compliance_risk"])}**
- **事件审计总结**: *{mask_markdown_text(data["audit_summary"])}*

## 二、RAG 语义匹配合规基准条款
"""
        if retrieved_docs:
            md_content += f"在本次审计中，语义数据库成功为您提取了最相近的 {len(retrieved_docs)} 条合规基线规范：\n"
            for i, doc in enumerate(retrieved_docs):
                md_content += (
                    f"\n> **参考规范 {i + 1}**:\n{markdown_quote_block(mask_markdown_text(doc))}\n"
                )
        else:
            md_content += (
                f"> ⚠️ **警告**：RAG 语义检索未命中任何合规条款（当前阈值 "
                f"`RELEVANCE_THRESHOLD={RELEVANCE_THRESHOLD}`）。\n"
                "> 本次审计在**无合规参考基准**的情况下完成，结论仅供参考。\n"
                "> 建议适当调高 `.env` 中的 `RELEVANCE_THRESHOLD` 或补充合规手册内容。\n"
            )

        md_content += """
## 三、提取指派的任务看板
根据对会议内容的解析，自动生成的结构化处理任务如下：

| 序号 | 任务名称 | 负责人 | 优先级 | 截止日期 | 审计生成时间 |
| :--- | :--- | :--- | :--- | :--- | :--- |
"""
        for idx, (_, row) in enumerate(task_output_df.iterrows(), 1):
            md_content += (
                f"| {idx} | {markdown_table_cell(mask_markdown_text(row.get('task_name')))} | "
                f"{markdown_table_cell(mask_markdown_text(row.get('owner')))} | "
                f"{markdown_table_cell(mask_markdown_text(row.get('priority')))} | "
                f"{markdown_table_cell(row.get('due_date'))} | "
                f"{markdown_table_cell(row.get('audit_time'))} |\n"
            )

        md_content += """
## 四、数据合规与流程治理风险
"""
        if risk_output_df.empty:
            md_content += "\n> 未检测到确定性数据治理风险项。\n"
        else:
            md_content += """
| 序号 | 风险类型 | 等级 | 证据 | 整改建议 | 人工复核 |
| :--- | :--- | :--- | :--- | :--- | :--- |
"""
            for idx, (_, row) in enumerate(risk_output_df.iterrows(), 1):
                manual_review = "是" if str(row.get("manual_review_required", "")).lower() in ("true", "1", "yes") else "否"
                md_content += (
                    f"| {idx} | {markdown_table_cell(mask_markdown_text(row.get('risk_type')))} | "
                    f"{markdown_table_cell(mask_markdown_text(row.get('severity')))} | "
                    f"{markdown_table_cell(mask_markdown_text(row.get('evidence_masked')))} | "
                    f"{markdown_table_cell(mask_markdown_text(row.get('recommendation')))} | "
                    f"{manual_review} |\n"
                )

        # 附加原始文本摘要
        excerpt = mask_markdown_text(content[:500] + ("..." if len(content) > 500 else ""))
        md_content += f"""
## 五、会议原始文本摘要
以下为本次审计的原始输入片段（截取前 500 字）：

```text
{excerpt}
```
"""

        # 保存 Markdown 报告
        md_path = unique_file_path(
            os.path.join(OUTPUT, f"{base_name}_{time_suffix}_audit_report.md")
        )
        with open(md_path, "w", encoding="utf-8") as f_md:
            f_md.write(md_content)

        record_audit_history(
            source_file=source_file,
            audit_time=audit_time,
            task_output_df=task_output_df,
            risk_output_df=risk_output_df,
            tasks_csv_path=csv_path,
            risk_csv_path=risk_csv_path,
            report_path=md_path,
            mode=COMPLIANCE_MODE,
        )

        logging.info(
            f"工作流【{os.path.basename(file_path)}】处理成功！结果已保存至 output/ 目录。"
        )
        return ProcessResult(
            success=True,
            tasks_csv_path=csv_path,
            risk_csv_path=risk_csv_path,
            report_path=md_path,
            mode=COMPLIANCE_MODE,
        )

    except Exception as e:
        logging.exception(f"文件处理失败: {e}")
        if full_response:
            masked_preview = audit_rules.mask_sensitive_evidence(full_response[:500])
            logging.error(
                f"--- 原始模型输出预览 --- \n{masked_preview}\n------------------------"
            )
        return ProcessResult(success=False)


def process_file(file_path, collection, progress_prefix="", mode: str = COMPLIANCE_MODE):
    result = process_file_with_result(file_path, collection, progress_prefix, mode=mode)
    return result.success


# 检查并自动构建文件夹环境
def check_environment():
    dirs = [
        INBOX,
        OUTPUT,
        ARCHIVE,
        FAILED,
        CONFIG_DIR,
        SEMICONDUCTOR_IP_CONFIG_DIR,
        VECTOR_STORE_DIR,
    ]
    for d in dirs:
        os.makedirs(d, exist_ok=True)


def check_ollama_status() -> dict[str, Any]:
    """
    检查 Ollama 是否在线，以及所需的 AUDIT_MODEL 和 EMBED_MODEL 是否可用。
    """
    try:
        client = ollama.Client()
        models_info = client.list()
        models = []
        for m in models_info.get("models", []):
            name = (
                getattr(m, "model", None)
                or (m.get("model") if isinstance(m, dict) else None)
                or str(m)
            )
            models.append(name)
        
        audit_model_ok = AUDIT_MODEL in models or any(m.startswith(AUDIT_MODEL + ":") for m in models)
        embed_model_ok = EMBED_MODEL in models or any(m.startswith(EMBED_MODEL + ":") for m in models)
        
        return {
            "connected": True,
            "audit_model_ok": audit_model_ok,
            "embed_model_ok": embed_model_ok,
            "models": models,
            "error": ""
        }
    except Exception as e:
        return {
            "connected": False,
            "audit_model_ok": False,
            "embed_model_ok": False,
            "models": [],
            "error": str(e)
        }


def rebuild_knowledge_base(mode: str = COMPLIANCE_MODE):
    """
    强制删除现有 collection 并重新构建向量库。
    """
    mode = normalize_audit_mode(mode)
    client = chromadb.PersistentClient(path=VECTOR_STORE_DIR)
    try:
        client.delete_collection(name=collection_name_for_mode(mode))
    except Exception:
        pass
    return initialize_knowledge_base(mode)


def wait_for_file_ready(file_path, timeout=5, stable_interval=0.5):
    """
    等待文件写入完成：如果在 stable_interval 内文件大小没有变化，则认为写入完成。
    """
    if not os.path.exists(file_path):
        return False
    start_time = time.time()
    last_size = -1
    while time.time() - start_time < timeout:
        try:
            current_size = os.path.getsize(file_path)
            if current_size == last_size and current_size > 0:
                return True
            last_size = current_size
        except Exception:
            pass
        time.sleep(stable_interval)
    return False


def check_exit_or_sleep(timeout=POLL_INTERVAL):
    """
    非阻塞检查用户是否按下了 ESC 键。
    """
    fd = sys.stdin.fileno()
    is_tty = os.isatty(fd)

    if not is_tty:
        time.sleep(timeout)
        return False

    try:
        import select
        import termios
        import tty
    except ImportError:
        time.sleep(timeout)
        return False

    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        start_time = time.time()
        while time.time() - start_time < timeout:
            rlist, _, _ = select.select([sys.stdin], [], [], 0.05)
            if rlist:
                ch = sys.stdin.read(1)
                if ch == "\x1b":  # ESC 键
                    r_next, _, _ = select.select([sys.stdin], [], [], 0.02)
                    if not r_next:
                        return True
                    else:
                        while True:
                            rn, _, _ = select.select([sys.stdin], [], [], 0.01)
                            if rn:
                                sys.stdin.read(1)
                            else:
                                break
            time.sleep(0.05)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    return False


# Watchdog 文件夹事件监听
class InboxFileHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            self._handle_path(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._handle_path(event.dest_path)

    def _handle_path(self, path):
        if path.endswith(".txt"):
            abs_path = os.path.abspath(path)
            with _queue_lock:
                if abs_path in _queued_paths:
                    logging.debug(f"跳过重复文本事件: {os.path.basename(path)}")
                    return
                _queued_paths.add(abs_path)
            logging.info(f"检测到新待审文本: {os.path.basename(path)}")
            file_queue.put(abs_path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Offline Auto Audit.")
    parser.add_argument(
        "--mode",
        choices=sorted(SUPPORTED_AUDIT_MODES),
        default=os.getenv("AUDIT_MODE", COMPLIANCE_MODE),
        help="Analysis mode. Defaults to AUDIT_MODE or compliance.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    audit_mode = normalize_audit_mode(args.mode)
    check_environment()
    
    ollama_status = check_ollama_status()
    if not ollama_status["connected"]:
        logging.error(f"❌ 无法连接到 Ollama 服务，请确认 Ollama 正在运行！错误: {ollama_status['error']}")
    else:
        if not ollama_status["audit_model_ok"]:
            logging.warning(f"⚠️ 审计模型 {AUDIT_MODEL} 未在 Ollama 中下载，运行可能会报错，建议运行 'ollama pull {AUDIT_MODEL}'。")
        if not ollama_status["embed_model_ok"]:
            logging.warning(f"⚠️ 向量模型 {EMBED_MODEL} 未在 Ollama 中下载，运行可能会报错，建议运行 'ollama pull {EMBED_MODEL}'。")

    logging.info("正在连接本地向量库与初始化知识库...")
    collection = initialize_knowledge_base(audit_mode)

    logging.info("==========================================================")
    logging.info("🚀  本地 RAG 自动合规审计服务已就绪 (Event-Driven Edition)")
    logging.info(f"   分析模式 : {audit_mode}")
    logging.info(f"   审计模型 : {AUDIT_MODEL}")
    logging.info(f"   嵌入模型 : {EMBED_MODEL}")
    logging.info(f"   语义阈值 : {RELEVANCE_THRESHOLD}")
    logging.info("   监听目录 : inbox/")
    logging.info("==========================================================")
    logging.info("按 ESC 键安全退出，按 Ctrl+C 强制退出。")

    # 1. 扫描启动时已存在的文件并加入队列
    startup_files = sorted(
        os.path.join(INBOX, f) for f in os.listdir(INBOX) if f.endswith(".txt")
    )
    if startup_files:
        logging.info(
            f"扫描到启动时已存在的待审文件 {len(startup_files)} 个，加入待处理队列。"
        )
        for f in startup_files:
            abs_f = os.path.abspath(f)
            with _queue_lock:
                _queued_paths.add(abs_f)
            file_queue.put(abs_f)

    # 2. 启动 watchdog 监听 inbox 目录
    event_handler = InboxFileHandler()
    observer = Observer()
    observer.schedule(event_handler, path=INBOX, recursive=False)
    observer.start()

    try:
        while True:
            if not file_queue.empty():
                full_path = file_queue.get()
                try:
                    if os.path.exists(full_path):
                        if not wait_for_file_ready(full_path):
                            logging.warning(
                                f"文本文件写入不稳定，跳过审计: {os.path.basename(full_path)}"
                            )
                            continue
                        try:
                            # 对单个文件执行处理
                            success = process_file(full_path, collection, mode=audit_mode)

                            if success:
                                safe_move(full_path, ARCHIVE)
                            else:
                                safe_move(full_path, FAILED)
                                logging.warning(
                                    f"文件【{os.path.basename(full_path)}】审计失败，已移至 failed/ 目录。"
                                )
                        except Exception as process_err:
                            logging.error(
                                f"处理或移动文件 {full_path} 时发生严重错误: {process_err}"
                            )
                            try:
                                safe_move(full_path, FAILED)
                            except Exception as move_err:
                                logging.error(
                                    f"无法将已损坏文件移至 failed/ 目录: {move_err}"
                                )
                finally:
                    with _queue_lock:
                        _queued_paths.discard(os.path.abspath(full_path))
                    # 无论处理成功/失败/safe_move 本身抛异常，都必须调用 task_done
                    # 否则 queue.join() 将永久阻塞
                    file_queue.task_done()

                # 处理完后快速检查退出按键
                if check_exit_or_sleep(0.1):
                    logging.info("检测到 ESC 键，正在退出...")
                    break
            else:
                # 队列空闲时轮询检测 ESC
                if check_exit_or_sleep(POLL_INTERVAL):
                    logging.info("检测到 ESC 键，正在退出...")
                    break
    except KeyboardInterrupt:
        logging.info("收到 Ctrl+C 信号，强制退出。")
    finally:
        observer.stop()
        observer.join()
        logging.info("合规审计服务已安全关闭。")
