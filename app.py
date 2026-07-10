import argparse
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
from typing import Any

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
from audit_core.knowledge_base import (
    initialize_knowledge_base,
    rebuild_knowledge_base,
    retrieve_relevant_context,
)
from audit_core.model_io import check_environment, check_ollama_status
from audit_core.history import record_audit_history
from audit_core.text_processing import (
    bound_audit_prompt_content,
    count_tokens,
    recursive_split_text,
)
from capabilities.patent_research.legacy_analysis import (
    SEMICONDUCTOR_CLAIM_COLUMNS,
    SEMICONDUCTOR_IP_DISCLAIMER,
    SEMICONDUCTOR_RISK_COLUMNS,
    build_semiconductor_ip_system_prompt,
    render_semiconductor_ip_report,
    validate_semiconductor_ip_result,
    write_semiconductor_ip_outputs,
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
                output_dir=OUTPUT,
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
            output_dir=OUTPUT,
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
