import os
import json
import time
import datetime
import sys
import select
import shutil
import asyncio
import termios
import tty
import queue
import logging
import threading
import pandas as pd
import ollama
import chromadb
from dotenv import load_dotenv
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ──────────────────────────────────────────────
# 环境初始化与日志
# ──────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INBOX = os.path.join(BASE_DIR, "inbox")
OUTPUT = os.path.join(BASE_DIR, "output")
ARCHIVE = os.path.join(BASE_DIR, "archive")
FAILED = os.path.join(BASE_DIR, "failed")
CONFIG_DIR = os.path.join(BASE_DIR, "config", "compliance_rules")
VECTOR_STORE_DIR = os.path.join(BASE_DIR, "vector_store")

# 并发与重试
EMBEDDING_CONCURRENCY = int(os.getenv("EMBEDDING_CONCURRENCY", "2"))
EMBEDDING_MAX_RETRIES = int(os.getenv("EMBEDDING_MAX_RETRIES", "3"))

# 模型配置
AUDIT_MODEL = os.getenv("AUDIT_MODEL", "qwen3.5:9b")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
RELEVANCE_THRESHOLD = float(os.getenv("RELEVANCE_THRESHOLD", "0.5"))

# 事件循环与队列
file_queue = queue.Queue()
_queued_paths: set[str] = set()
_queue_lock = threading.Lock()
POLL_INTERVAL = 0.5  # 键盘检测间隔

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

*Please keep your internal thinking process (thinking) extremely concise, brief, and to the point. Do not write excessively long analysis in the thinking phase.*

OUTPUT FORMAT:
You MUST reply strictly in the following JSON format. Do not include any markdown formatting like "```json", do not include any preamble, and do not include any post-summary. Your entire response must be a single, parsable JSON object:

{{
  "compliance_risk": "高/中/低（给出清晰的中文违规定性与具体的合规条款判定说明）",
  "audit_summary": "一句话中文总结本次技术事件",
  "tasks": [
    {{
      "task_name": "标准化后的中文待办任务描述 (在此处纠正技术名词拼写错误)",
      "owner": "负责人姓名，若无明确负责人则为 'Unassigned'",
      "priority": "High/Medium/Low"
    }}
  ]
}}"""

def count_tokens(text):
    """
    原生轻量级 Token 估算：
    中文字符每个算 1 个 Token；英文单词按空格切分每个算 1 个 Token。
    """
    chinese_count = sum(1 for char in text if '\u4e00' <= char <= '\u9fff')
    english_words = len([
        w for w in text.split()
        if w and not any('\u4e00' <= c <= '\u9fff' for c in w)
    ])
    return chinese_count + english_words

def recursive_split_text(text, chunk_size=500, chunk_overlap=200):
    """
    模拟 RecursiveCharacterTextSplitter 原生实现。
    根据层级（段落 -> 换行 -> 空格）递归切分文本，并保留指定的重叠区。
    """
    separators = ["\n\n", "\n", " ", ""]

    def split(text_to_split, seps):
        if not seps:
            return [text_to_split]
        sep = seps[0]
        if sep == "":
            return [
                text_to_split[i:i + chunk_size]
                for i in range(0, len(text_to_split), chunk_size)
            ]

        parts = text_to_split.split(sep)
        chunks = []
        current_chunk = []
        current_size = 0

        for part in parts:
            part_size = count_tokens(part)
            if part_size > chunk_size:
                if current_chunk:
                    chunks.append(sep.join(current_chunk))
                    current_chunk = []
                    current_size = 0
                sub_parts = split(part, seps[1:])
                chunks.extend(sub_parts)
            elif current_size + part_size + (count_tokens(sep) if current_chunk else 0) <= chunk_size:
                current_chunk.append(part)
                current_size += part_size + (count_tokens(sep) if len(current_chunk) > 1 else 0)
            else:
                if current_chunk:
                    chunks.append(sep.join(current_chunk))
                current_chunk = [part]
                current_size = part_size
        if current_chunk:
            chunks.append(sep.join(current_chunk))
        return chunks

    raw_chunks = split(text, separators)

    merged_chunks = []
    current_group = []
    current_size = 0

    for chunk in raw_chunks:
        chunk_size_val = count_tokens(chunk)
        if current_size + chunk_size_val <= chunk_size:
            current_group.append(chunk)
            current_size += chunk_size_val
        else:
            if current_group:
                merged_chunks.append("\n".join(current_group))

            overlap_group = []
            overlap_size = 0
            for c in reversed(current_group):
                c_size = count_tokens(c)
                if overlap_size + c_size <= chunk_overlap:
                    overlap_group.insert(0, c)
                    overlap_size += c_size
                else:
                    break
            current_group = overlap_group + [chunk]
            current_size = overlap_size + chunk_size_val

    if current_group:
        merged_chunks.append("\n".join(current_group))

    return merged_chunks

async def _fetch_embeddings_async(documents):
    """
    使用有限并发批量获取文档向量。
    """
    client = ollama.AsyncClient()
    semaphore = asyncio.Semaphore(max(1, EMBEDDING_CONCURRENCY))

    async def embed_one(doc):
        async with semaphore:
            for attempt in range(EMBEDDING_MAX_RETRIES):
                try:
                    res = await client.embeddings(model=EMBED_MODEL, prompt=doc)
                    return res['embedding']
                except Exception:
                    if attempt == EMBEDDING_MAX_RETRIES - 1:
                        raise
                    await asyncio.sleep(1 + attempt)

    return await asyncio.gather(*(embed_one(doc) for doc in documents))

def initialize_knowledge_base():
    """
    检查并初始化 ChromaDB 本地向量库。如果库中无数据，读取 config 下合规手册切片后写入。
    """
    client = chromadb.PersistentClient(path=VECTOR_STORE_DIR)
    collection = client.get_or_create_collection(name="compliance_rules")

    if collection.count() > 0:
        logging.info("本地向量数据库已检测到合规数据，跳过向量库构建。")
        return collection

    logging.info("本地向量数据库为空，开始读取合规手册...")

    files = sorted([f for f in os.listdir(CONFIG_DIR) if f.endswith('.txt')])
    if not files:
        default_rule_path = os.path.join(CONFIG_DIR, "standard_pmo_compliance_rules.txt")
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
        with open(default_rule_path, 'w', encoding='utf-8') as f_def:
            f_def.write(default_rules)
        files = [os.path.basename(default_rule_path)]
        logging.info(f"已为您自动生成默认合规规范文本: {default_rule_path}")

    documents = []
    ids = []
    chunk_counter = 0

    for file in files:
        file_path = os.path.join(CONFIG_DIR, file)
        with open(file_path, 'r', encoding='utf-8') as f_in:
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

    logging.info(f"已生成 {len(documents)} 个合规切片，正在并发调用 {EMBED_MODEL} 写入本地 ChromaDB...")

    embeddings = asyncio.run(_fetch_embeddings_async(documents))

    collection.add(
        ids=ids,
        documents=documents,
        embeddings=embeddings
    )
    logging.info("向量数据库构建成功！数据已持久化。")
    return collection

def retrieve_relevant_context(collection, query_text, top_k=3):
    """
    语义检索网关：将查询日志转为向量并在本地 collection 中检索出最相关的 top_k 条合规基准，
    利用 RELEVANCE_THRESHOLD 进行相关度度量过滤。
    """
    res = ollama.embeddings(model=EMBED_MODEL, prompt=query_text)
    query_embedding = res['embedding']

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
        include=["documents", "distances"]
    )

    retrieved_docs = []
    if results and 'documents' in results and results['documents']:
        docs = results['documents'][0]
        distances = results.get('distances', [[]])[0]
        for doc, dist in zip(docs, distances):
            if dist < RELEVANCE_THRESHOLD:
                retrieved_docs.append(doc)
            else:
                logging.info(f"过滤低相关度基准 (distance={dist:.4f} >= threshold={RELEVANCE_THRESHOLD}): {doc[:30]}...")
    if not retrieved_docs:
        logging.warning(
            f"RAG 未检索到任何相关合规条款（threshold={RELEVANCE_THRESHOLD}），"
            "审计将在无参考基准的情况下进行。可适当调高 RELEVANCE_THRESHOLD。"
        )
    return retrieved_docs

def safe_move(src_path, dest_dir, timestamp_func=None):
    """
    将文件安全移动至指定目录：
    - 若目标已有同名文件，自动追加时间戳后缀避免覆盖。
    """
    if timestamp_func is None:
        timestamp_func = lambda: time.strftime("%Y%m%d_%H%M%S")

    filename = os.path.basename(src_path)
    dest_path = os.path.join(dest_dir, filename)

    if os.path.exists(dest_path):
        base, ext = os.path.splitext(filename)
        timestamp = timestamp_func()
        dest_path = os.path.join(dest_dir, f"{base}_{timestamp}{ext}")
        counter = 1
        while os.path.exists(dest_path):
            dest_path = os.path.join(dest_dir, f"{base}_{timestamp}_{counter}{ext}")
            counter += 1

    shutil.move(src_path, dest_path)
    return dest_path

def extract_json_object(response_text):
    """
    从模型输出中提取第一个完整 JSON 对象，兼容 think 标签和代码块。
    """
    text = response_text.strip()
    if "</think>" in text:
        text = text.split("</think>")[-1].strip()
    if "```json" in text:
        text = text.split("```json", 1)[1]
    elif "```" in text:
        text = text.split("```", 1)[1]
    if "```" in text:
        text = text.split("```", 1)[0]

    start = text.find("{")
    if start == -1:
        raise ValueError("模型输出中未找到 JSON 对象起始符")

    depth = 0
    in_string = False
    escape_next = False
    for idx in range(start, len(text)):
        char = text[idx]
        if escape_next:
            escape_next = False
            continue
        if char == "\\" and in_string:
            escape_next = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1].strip()

    raise ValueError("模型输出中的 JSON 对象不完整")

def markdown_table_cell(value):
    """
    转义 Markdown 表格单元格。
    """
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")

def markdown_quote_block(text):
    return "\n".join(f"> {line}" if line else ">" for line in text.strip().splitlines())

def process_file(file_path, collection, progress_prefix=""):
    """
    对单个文件执行完整的 RAG 审计流程。
    """
    full_response = ""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # 1. 语义检索合规基准
        retrieved_docs = retrieve_relevant_context(collection, content, top_k=3)
        compliance_context = "\n\n".join([f"【条款 {i+1}】:\n{doc}" for i, doc in enumerate(retrieved_docs)])

        # 2. 组装 SYSTEM PROMPT
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(compliance_context=compliance_context)

        # 3. 本地大模型推理并流式监听
        logging.info(f"大模型分析中 (模型: {AUDIT_MODEL})，如需强制退出请按 Ctrl+C")
        response_stream = ollama.generate(
            model=AUDIT_MODEL,
            prompt=content,
            system=system_prompt,
            options={
                "temperature": 0.1,
                "num_ctx": 8192,
                "num_keep": 0
            },
            stream=True
        )

        spinner = ['|', '/', '-', '\\']
        spinner_idx = 0

        if not progress_prefix:
            progress_prefix = f"正在分析: {os.path.basename(file_path)}"

        for chunk in response_stream:
            token = chunk.get('response', '')
            full_response += token
            print(f"\r{progress_prefix}... {spinner[spinner_idx]} ", end="", flush=True)
            spinner_idx = (spinner_idx + 1) % len(spinner)

        print(f"\r{progress_prefix}... 完成!   \n")

        # 4. 模型响应过滤与容错 JSON 解析
        data = json.loads(extract_json_object(full_response))
        if not isinstance(data.get('tasks'), list):
            data['tasks'] = []
        data.setdefault('compliance_risk', '未知')
        data.setdefault('audit_summary', '模型未返回审计总结')

        # 5. 数据清洗与加工 (CSV/Pandas)
        df = pd.DataFrame(data['tasks'])
        for column, default in {
            'task_name': '未知任务',
            'owner': 'Unassigned',
            'priority': 'Medium',
        }.items():
            if column not in df.columns:
                df[column] = default
        df = df.drop_duplicates().reset_index(drop=True)  # reset_index 保证 iterrows 序号连续

        df['owner'] = df['owner'].fillna("Unassigned").replace({"": "Unassigned"})
        df['task_name'] = df['task_name'].fillna("未知任务").replace({"": "未知任务"})
        df['priority'] = df['priority'].fillna("Medium").replace({"": "Medium"})
        df['audit_time'] = time.strftime("%Y-%m-%d %H:%M:%S")
        
        # 为 Jira 兼容拓展字段
        df['source_file'] = os.path.basename(file_path)
        
        due_dates = []
        today = datetime.date.today()
        for p in df['priority']:
            p_lower = str(p).lower()
            if 'high' in p_lower:
                days = 3
            elif 'low' in p_lower:
                days = 7
            else:
                days = 5
            due_dates.append((today + datetime.timedelta(days=days)).isoformat())
        df['due_date'] = due_dates

        base_name = os.path.splitext(os.path.basename(file_path))[0]
        time_suffix = time.strftime("%Y-%m-%d_%H_%M")

        # 保存 CSV 任务指派表
        csv_path = os.path.join(OUTPUT, f"{base_name}_{time_suffix}.csv")
        df.to_csv(csv_path, index=False, encoding='utf-8-sig')

        # 6. 生成排版美观的 Markdown 合规审计报告
        md_content = f"""# 自动化合规审计与任务指派报告

## 一、基础审计信息
- **被处理文件**: `{os.path.basename(file_path)}`
- **审计结束时间**: `{time.strftime('%Y-%m-%d %H:%M:%S')}`
- **合规风险评估**: **{data['compliance_risk']}**
- **事件审计总结**: *{data['audit_summary']}*

## 二、RAG 语义匹配合规基准条款
"""
        if retrieved_docs:
            md_content += f"在本次审计中，语义数据库成功为您提取了最相近的 {len(retrieved_docs)} 条合规基线规范：\n"
            for i, doc in enumerate(retrieved_docs):
                md_content += f"\n> **参考规范 {i+1}**:\n{markdown_quote_block(doc)}\n"
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
        for i, row in df.iterrows():
            md_content += (
                f"| {i+1} | {markdown_table_cell(row.get('task_name'))} | "
                f"{markdown_table_cell(row.get('owner'))} | "
                f"{markdown_table_cell(row.get('priority'))} | "
                f"{markdown_table_cell(row.get('due_date'))} | "
                f"{markdown_table_cell(row.get('audit_time'))} |\n"
            )

        # 附加原始文本摘要
        excerpt = content[:500] + ("..." if len(content) > 500 else "")
        md_content += f"""
## 四、会议原始文本摘要
以下为本次审计的原始输入片段（截取前 500 字）：

```text
{excerpt}
```
"""

        # 保存 Markdown 报告
        md_path = os.path.join(OUTPUT, f"{base_name}_{time_suffix}.md")
        with open(md_path, 'w', encoding='utf-8') as f_md:
            f_md.write(md_content)

        logging.info(f"工作流【{os.path.basename(file_path)}】处理成功！结果已保存至 output/ 目录。")
        return True

    except Exception as e:
        logging.exception(f"文件处理失败: {e}")
        if full_response:
            logging.error(f"--- 原始模型输出预览 --- \n{full_response[:500]}\n------------------------")
        return False

# 检查并自动构建文件夹环境
def check_environment():
    dirs = [INBOX, OUTPUT, ARCHIVE, FAILED, CONFIG_DIR, VECTOR_STORE_DIR]
    for d in dirs:
        os.makedirs(d, exist_ok=True)

def check_exit_or_sleep(timeout=POLL_INTERVAL):
    """
    非阻塞检查用户是否按下了 ESC 键。
    """
    fd = sys.stdin.fileno()
    is_tty = os.isatty(fd)

    if not is_tty:
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
                if ch == '\x1b':  # ESC 键
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
        if path.endswith('.txt'):
            abs_path = os.path.abspath(path)
            with _queue_lock:
                if abs_path in _queued_paths:
                    logging.debug(f"跳过重复文本事件: {os.path.basename(path)}")
                    return
                _queued_paths.add(abs_path)
            logging.info(f"检测到新待审文本: {os.path.basename(path)}")
            file_queue.put(abs_path)

if __name__ == "__main__":
    check_environment()
    logging.info("正在连接本地向量库与初始化知识库...")
    collection = initialize_knowledge_base()

    logging.info("==========================================================")
    logging.info("🚀  本地 RAG 自动合规审计服务已就绪 (Event-Driven Edition)")
    logging.info(f"   审计模型 : {AUDIT_MODEL}")
    logging.info(f"   嵌入模型 : {EMBED_MODEL}")
    logging.info(f"   语义阈值 : {RELEVANCE_THRESHOLD}")
    logging.info(f"   监听目录 : inbox/")
    logging.info("==========================================================")
    logging.info("按 ESC 键安全退出，按 Ctrl+C 强制退出。")

    # 1. 扫描启动时已存在的文件并加入队列
    startup_files = sorted(
        os.path.join(INBOX, f)
        for f in os.listdir(INBOX)
        if f.endswith('.txt')
    )
    if startup_files:
        logging.info(f"扫描到启动时已存在的待审文件 {len(startup_files)} 个，加入待处理队列。")
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
                        try:
                            # 对单个文件执行处理
                            success = process_file(full_path, collection)

                            if success:
                                safe_move(full_path, ARCHIVE)
                            else:
                                safe_move(full_path, FAILED)
                                logging.warning(f"文件【{os.path.basename(full_path)}】审计失败，已移至 failed/ 目录。")
                        except Exception as process_err:
                            logging.error(f"处理或移动文件 {full_path} 时发生严重错误: {process_err}")
                            try:
                                safe_move(full_path, FAILED)
                            except Exception as move_err:
                                logging.error(f"无法将已损坏文件移至 failed/ 目录: {move_err}")
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
