import os
import json
import time
import sys
import select
import shutil
import asyncio
import termios
import tty
import pandas as pd
import ollama
import chromadb

# 配置本地物理路径
BASE_DIR = "."
INBOX = os.path.join(BASE_DIR, "inbox")
OUTPUT = os.path.join(BASE_DIR, "output")
ARCHIVE = os.path.join(BASE_DIR, "archive")
FAILED = os.path.join(BASE_DIR, "failed")
CONFIG_DIR = os.path.join(BASE_DIR, "config", "compliance_rules")
VECTOR_STORE_DIR = os.path.join(BASE_DIR, "vector_store")

# 严密的 SYSTEM PROMPT 模板，包含动态嵌入的合规标准上下文
SYSTEM_PROMPT_TEMPLATE = """You are an expert PMO (Project Management Office) Compliance Auditor and Technical Operations Analyst. Your job is to analyze messy, unformatted technical meeting notes or logs, perform a strict compliance check against standard software engineering workflows using the provided compliance standards, and extract actionable tasks.

### 核心合规参考标准（必须作为审计违规判断与任务抽取的唯一基准）：
{compliance_context}

### 审计审计与任务提取规则：
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
    中文字符每个算 1 个 Token；英文单词（不含中文字符的词）按空格切分每个算 1 个 Token。
    修复：避免中文字符被 split() 二次计数。
    """
    chinese_count = sum(1 for char in text if '\u4e00' <= char <= '\u9fff')
    # 只统计不包含任何中文字符的纯英文/数字词
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
        parts = text_to_split.split(sep)
        chunks = []
        current_chunk = []
        current_size = 0

        for part in parts:
            part_size = count_tokens(part)
            if part_size > chunk_size:
                # 若单部分超长，递归下一级分隔符
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

    # 基础语义切片
    raw_chunks = split(text, separators)

    # 结合 overlap 机制进行合并
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

            # 计算重叠区保留部分
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
    优化：使用异步并发方式批量获取所有文档的向量，替代串行逐个请求。
    对 20 个 chunk 的知识库初始化，速度可提升数倍。
    """
    client = ollama.AsyncClient()
    tasks = [client.embeddings(model="nomic-embed-text", prompt=doc) for doc in documents]
    results = await asyncio.gather(*tasks)
    return [res['embedding'] for res in results]

def initialize_knowledge_base():
    """
    检查并初始化 ChromaDB 本地向量库。如果库中无数据，读取 config 下合规手册切片后写入。
    """
    client = chromadb.PersistentClient(path=VECTOR_STORE_DIR)
    collection = client.get_or_create_collection(name="compliance_rules")

    if collection.count() > 0:
        print("本地向量数据库已检测到合规数据，跳过向量库构建。")
        return collection

    print("本地向量数据库为空，开始读取合规手册...")

    # 获取需要处理的合规文本文件（优化：sorted 保证加载顺序稳定）
    files = sorted([f for f in os.listdir(CONFIG_DIR) if f.endswith('.txt')])
    if not files:
        # 自动生成默认的合规规范文件，确保首次运行时零配置上手
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
        print(f"已为您自动生成默认合规规范文本: {default_rule_path}")

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
        print("未提取到任何有效切片文本。")
        return collection

    print(f"已生成 {len(documents)} 个合规切片，正在并发调用 nomic-embed-text 写入本地 ChromaDB...")

    # 优化：并发异步获取所有向量，替代串行逐个请求
    embeddings = asyncio.run(_fetch_embeddings_async(documents))

    collection.add(
        ids=ids,
        documents=documents,
        embeddings=embeddings
    )
    print("向量数据库构建成功！数据已持久化。")
    return collection

def retrieve_relevant_context(collection, query_text, top_k=3):
    """
    语义检索网关：将查询日志转为向量并在本地 collection 中检索出最相关的 top_k 条合规基准
    """
    res = ollama.embeddings(model="nomic-embed-text", prompt=query_text)
    query_embedding = res['embedding']

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k
    )

    retrieved_docs = []
    if results and 'documents' in results and results['documents']:
        retrieved_docs = results['documents'][0]
    return retrieved_docs

def safe_move(src_path, dest_dir):
    """
    将文件安全移动至指定目录：
    - 使用 shutil.move() 替代 os.rename()，兼容跨文件系统移动。
    - 若目标已有同名文件，自动追加时间戳后缀避免静默覆盖。
    """
    filename = os.path.basename(src_path)
    dest_path = os.path.join(dest_dir, filename)

    if os.path.exists(dest_path):
        base, ext = os.path.splitext(filename)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        dest_path = os.path.join(dest_dir, f"{base}_{timestamp}{ext}")

    shutil.move(src_path, dest_path)

def process_file(file_path, collection, progress_prefix=""):
    """
    对单个文件执行完整的 RAG 审计流程。
    返回 True 表示处理成功并已写入报告，返回 False 表示处理失败（文件不应被归档）。
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # 1. 语义检索合规基准
    retrieved_docs = retrieve_relevant_context(collection, content, top_k=3)
    compliance_context = "\n\n".join([f"【条款 {i+1}】:\n{doc}" for i, doc in enumerate(retrieved_docs)])

    # 2. 组装 SYSTEM PROMPT
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(compliance_context=compliance_context)

    # 3. 本地大模型推理并流式监听
    print(f"  提示：模型分析中，如需强制退出请按 Ctrl+C")
    response_stream = ollama.generate(
        model='qwen3.5:9b',  # 本地运行的大模型大脑
        prompt=content,
        system=system_prompt,
        options={
            "temperature": 0.1,
            "num_ctx": 8192,  # 增大上下文窗口到 8K，避免推理链条长时被截断
            "num_keep": 0     # 防止 KV cache 污染，保证多次推理的格式稳定
        },
        stream=True
    )

    full_response = ""
    spinner = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
    spinner_idx = 0

    if not progress_prefix:
        progress_prefix = f"正在分析: {os.path.basename(file_path)}"

    for chunk in response_stream:
        token = chunk['response']
        full_response += token
        print(f"\r{progress_prefix}... {spinner[spinner_idx]} ", end="", flush=True)
        spinner_idx = (spinner_idx + 1) % len(spinner)

    print(f"\r{progress_prefix}... 完成!   \n")

    # 4. 模型响应过滤与容错 JSON 解析
    try:
        response_text = full_response.strip()

        # 过滤 <think> 标签（深度推理链）
        if "</think>" in response_text:
            response_text = response_text.split("</think>")[-1].strip()

        # 过滤 markdown 代码块包裹
        if response_text.startswith("```json"):
            response_text = response_text[7:]
        elif response_text.startswith("```"):
            response_text = response_text[3:]
        if response_text.endswith("```"):
            response_text = response_text[:-3]

        response_text = response_text.strip()

        # 解析 JSON
        data = json.loads(response_text)

        # 5. 数据清洗与加工 (CSV/Pandas)
        df = pd.DataFrame(data['tasks'])
        df = df.drop_duplicates()  # 清除偶发重复任务

        # 字段空值与默认值修正（修复：使用字典形式的 replace 保证跨版本兼容）
        if 'owner' in df.columns:
            df['owner'] = df['owner'].fillna("Unassigned").replace({"": "Unassigned"})
        if 'task_name' in df.columns:
            df['task_name'] = df['task_name'].fillna("未知任务").replace({"": "未知任务"})
        if 'priority' in df.columns:
            df['priority'] = df['priority'].fillna("Medium").replace({"": "Medium"})

        df['audit_time'] = time.strftime("%Y-%m-%d %H_%M_%S")

        # 命名格式：被处理的文件名+处理结束时的时间 (YYYY-MM-DD_HH:mm)
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
在本次审计中，语义数据库成功为您提取了最相近的 {len(retrieved_docs)} 条合规基线规范：
"""
        for i, doc in enumerate(retrieved_docs):
            md_content += f"\n> **参考规范 {i+1}**:\n> {doc.strip()}\n"

        md_content += """
## 三、提取指派的任务看板
根据对会议内容的解析，自动生成的结构化处理任务如下：

| 序号 | 任务名称 | 负责人 | 优先级 | 审计生成时间 |
| :--- | :--- | :--- | :--- | :--- |
"""
        for i, row in df.iterrows():
            md_content += f"| {i+1} | {row.get('task_name')} | {row.get('owner')} | {row.get('priority')} | {row.get('audit_time')} |\n"

        # 保存 Markdown 报告
        md_path = os.path.join(OUTPUT, f"{base_name}_{time_suffix}.md")
        with open(md_path, 'w', encoding='utf-8') as f_md:
            f_md.write(md_content)

        print(f"工作流【{os.path.basename(file_path)}】处理成功！文件已生成至 output 目录。")
        return True  # 明确返回成功标志，主循环凭此决定是否归档

    except Exception as e:
        print(f"解析失败，模型输出内容不符合JSON标准: {e}")
        print(f"--- 原始模型输出预览 --- \n{full_response[:500]}\n------------------------")
        return False  # 返回失败标志，主循环不会归档此文件

# 检查并自动构建文件夹环境
def check_environment():
    dirs = [INBOX, OUTPUT, ARCHIVE, FAILED, CONFIG_DIR, VECTOR_STORE_DIR]
    for d in dirs:
        os.makedirs(d, exist_ok=True)

def check_exit_or_sleep(timeout=3.0):
    """
    非阻塞检查用户是否按下了 ESC 键，并在没有按下时进行睡眠。
    在超时时间内轮询 stdin，如果检测到 ESC (ASCII 27)，则返回 True。
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
                    # 再次非阻塞检查，判定是否为方向键等 ANSI 序列，防止误触发
                    r_next, _, _ = select.select([sys.stdin], [], [], 0.02)
                    if not r_next:
                        return True
                    else:
                        # 消费掉后续的 ANSI 序列字符（例如 '[A'）
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

if __name__ == "__main__":
    check_environment()
    print("正在连接本地向量库与初始化知识库...")
    collection = initialize_knowledge_base()

    print("\n本地 RAG 自动化工作流已就绪，正在轮询监听 inbox 文件夹（随时可按 ESC 键退出）...")
    while True:
        try:
            # 优化：sorted() 保证文件处理顺序稳定（字母升序）
            files = sorted([f for f in os.listdir(INBOX) if f.endswith('.txt')])
            if files:
                total_files = len(files)
                for idx, file in enumerate(files):
                    full_path = os.path.join(INBOX, file)
                    processed_count = idx
                    pending_count = total_files - idx - 1
                    progress_prefix = f"[进度: 已分析 {processed_count} 个，待分析 {pending_count} 个] 正在分析: {file}"

                    success = process_file(full_path, collection, progress_prefix=progress_prefix)

                    # 处理成功 -> 归档至 archive；失败 -> 隔离至 failed，不再重试
                    if success:
                        safe_move(full_path, ARCHIVE)
                    else:
                        safe_move(full_path, FAILED)
                        print(f"文件【{file}】解析失败，已移至 failed 目录隔离，请人工检查后再决定是否重新投入 inbox。")

                print("\n当前所有文件已分析完成！审计结果已生成至 output 目录，且 inbox 中的原文件已全部移至 archive 目录归档。")
                print("您可以将新的待审计文件放入 inbox 目录中继续分析，或按 ESC 键退出脚本。\n")
            if check_exit_or_sleep(3.0):
                print("\n检测到 ESC 键，自动化工作流已安全退出。")
                break
        except KeyboardInterrupt:
            print("\n自动化工作流已被用户终止退出。")
            break
        except Exception as e:
            print(f"轮询过程发生异常: {e}")
            if check_exit_or_sleep(3.0):
                break