import asyncio
import logging
import os
from typing import Any, cast

import chromadb
import ollama

from .config import (
    COMPLIANCE_MODE,
    EMBEDDING_CONCURRENCY,
    EMBEDDING_MAX_RETRIES,
    EMBED_MODEL,
    RELEVANCE_THRESHOLD,
    SEMICONDUCTOR_IP_MODE,
    VECTOR_STORE_DIR,
    collection_name_for_mode,
    normalize_audit_mode,
    rules_dir_for_mode,
)
from .text_processing import count_tokens, recursive_split_text


async def _fetch_embeddings_async(documents: list[str]) -> list[list[float]]:
    client = ollama.AsyncClient()
    semaphore = asyncio.Semaphore(max(1, EMBEDDING_CONCURRENCY))

    async def embed_one(document: str) -> list[float]:
        async with semaphore:
            for attempt in range(EMBEDDING_MAX_RETRIES):
                try:
                    response = await client.embeddings(
                        model=EMBED_MODEL, prompt=document
                    )
                    return cast(list[float], response["embedding"])
                except Exception:
                    if attempt == EMBEDDING_MAX_RETRIES - 1:
                        raise
                    await asyncio.sleep(1 + attempt)
            raise RuntimeError("Unreachable")

    return await asyncio.gather(*(embed_one(document) for document in documents))


def initialize_knowledge_base(mode: str = COMPLIANCE_MODE):
    mode = normalize_audit_mode(mode)
    rules_dir = rules_dir_for_mode(mode)
    client = chromadb.PersistentClient(path=VECTOR_STORE_DIR)
    collection = client.get_or_create_collection(
        name=collection_name_for_mode(mode)
    )

    if collection.count() > 0:
        logging.info(
            "本地向量数据库已检测到 %s 数据，跳过向量库构建。", mode
        )
        return collection

    logging.info("本地向量数据库为空，开始读取 %s 规则...", mode)
    files = sorted(
        filename for filename in os.listdir(rules_dir) if filename.endswith(".txt")
    )
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
        with open(default_rule_path, "w", encoding="utf-8") as default_file:
            default_file.write(default_rules)
        files = [os.path.basename(default_rule_path)]
        logging.info("已为您自动生成默认合规规范文本: %s", default_rule_path)

    if not files and mode == SEMICONDUCTOR_IP_MODE:
        default_rule_path = os.path.join(
            rules_dir, "sic_power_device_terms.txt"
        )
        default_rules = """SiC功率半导体IP初筛规则：

1. 只基于输入文本和检索规则做技术情报整理，不输出法律意见。
2. 优先拆解权利要求或核心技术特征中的结构、材料、步骤、功能和技术效果。
3. 每个关键判断尽量给出原文证据；证据不足时标记人工复核。
4. 不得输出确定侵权、确定不侵权、专利有效或专利无效结论。
5. SiC MOSFET 分析重点包括 trench gate、drift layer、gate oxide reliability、on-resistance、breakdown voltage、thermal resistance 和 edge termination。
"""
        with open(default_rule_path, "w", encoding="utf-8") as default_file:
            default_file.write(default_rules)
        files = [os.path.basename(default_rule_path)]
        logging.info("已自动生成默认半导体IP规则文本: %s", default_rule_path)

    documents: list[str] = []
    ids: list[str] = []
    chunk_counter = 0
    for filename in files:
        file_path = os.path.join(rules_dir, filename)
        with open(file_path, "r", encoding="utf-8") as input_file:
            text_content = input_file.read()

        chunks = recursive_split_text(
            text_content, chunk_size=500, chunk_overlap=200
        )
        for chunk in chunks:
            if chunk.strip():
                documents.append(chunk)
                ids.append(f"chunk_{chunk_counter}")
                chunk_counter += 1

    if not documents:
        logging.warning("未提取到任何有效切片文本。")
        return collection

    logging.info(
        "已生成 %s 个 %s 切片，正在并发调用 %s 写入本地 ChromaDB...",
        len(documents),
        mode,
        EMBED_MODEL,
    )
    embeddings: Any = asyncio.run(_fetch_embeddings_async(documents))
    collection.add(ids=ids, documents=documents, embeddings=embeddings)
    logging.info("向量数据库构建成功！数据已持久化。")
    return collection


def retrieve_relevant_context(collection, query_text, top_k=3):
    if count_tokens(query_text) <= 500:
        texts_to_embed = [query_text]
    else:
        texts_to_embed = recursive_split_text(
            query_text, chunk_size=400, chunk_overlap=100
        )[:10]

    all_retrieved: list[tuple[str, float]] = []
    seen_docs: set[str] = set()
    for text in texts_to_embed:
        if not text.strip():
            continue
        try:
            response = ollama.embeddings(model=EMBED_MODEL, prompt=text)
            results = collection.query(
                query_embeddings=[response["embedding"]],
                n_results=top_k,
                include=["documents", "distances"],
            )
            if results and results.get("documents"):
                documents = results["documents"][0]
                distances = results.get("distances", [[]])[0]
                for document, distance in zip(documents, distances):
                    if distance < RELEVANCE_THRESHOLD and document not in seen_docs:
                        seen_docs.add(document)
                        all_retrieved.append((document, distance))
                    elif distance >= RELEVANCE_THRESHOLD:
                        logging.debug(
                            "过滤低相关度基准 (distance=%.4f >= threshold=%s): %s...",
                            distance,
                            RELEVANCE_THRESHOLD,
                            document[:30],
                        )
        except Exception as exc:
            logging.error("提取分片 Embedding 检索失败: %s", exc)

    all_retrieved.sort(key=lambda item: item[1])
    retrieved_docs = [document for document, _ in all_retrieved[:top_k]]
    if not retrieved_docs:
        logging.warning(
            "RAG 未检索到任何相关合规条款（threshold=%s），"
            "审计将在无参考基准的情况下进行。可适当调高 RELEVANCE_THRESHOLD。",
            RELEVANCE_THRESHOLD,
        )
    return retrieved_docs


def rebuild_knowledge_base(mode: str = COMPLIANCE_MODE):
    mode = normalize_audit_mode(mode)
    client = chromadb.PersistentClient(path=VECTOR_STORE_DIR)
    try:
        client.delete_collection(name=collection_name_for_mode(mode))
    except Exception:
        pass
    return initialize_knowledge_base(mode)
