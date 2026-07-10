import logging
import os
from collections.abc import Callable
from typing import Any

from capabilities.meeting_audit.legacy import SYSTEM_PROMPT_TEMPLATE
from capabilities.patent_research.legacy_analysis import (
    build_semiconductor_ip_system_prompt,
    validate_semiconductor_ip_result,
    write_semiconductor_ip_outputs,
)

from .artifacts import write_meeting_outputs
from .config import (
    AUDIT_MODEL,
    COMPLIANCE_MODE,
    OUTPUT,
    SEMICONDUCTOR_IP_MODE,
    generation_options_for_mode,
    input_token_limit_for_mode,
    normalize_audit_mode,
)
from .formatting import mask_markdown_text
from .history import record_audit_history
from .knowledge_base import retrieve_relevant_context
from .model_io import generate_json_stream
from .models import ProcessResult
from .text_processing import bound_audit_prompt_content


def process_file_with_result(
    file_path: str,
    collection,
    progress_prefix: str = "",
    cancel_checker: Callable[[], bool] | None = None,
    mode: str = COMPLIANCE_MODE,
    *,
    output_dir: str | None = None,
    json_generator: Callable[..., dict[str, Any]] = generate_json_stream,
) -> ProcessResult:
    mode = normalize_audit_mode(mode)
    selected_output_dir = output_dir or OUTPUT
    try:
        with open(file_path, "r", encoding="utf-8") as input_file:
            content = input_file.read()

        retrieved_docs = retrieve_relevant_context(collection, content, top_k=3)
        if mode == SEMICONDUCTOR_IP_MODE:
            system_prompt = build_semiconductor_ip_system_prompt(retrieved_docs)
        else:
            compliance_context = "\n\n".join(
                f"【条款 {index + 1}】:\n{document}"
                for index, document in enumerate(retrieved_docs)
            )
            system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
                compliance_context=compliance_context
            )

        logging.info(
            "大模型分析中 (模型: %s)，如需强制退出请按 Ctrl+C", AUDIT_MODEL
        )
        prompt = bound_audit_prompt_content(
            content, max_tokens=input_token_limit_for_mode(mode)
        )
        data = json_generator(
            model=AUDIT_MODEL,
            prompt=prompt,
            system=system_prompt,
            options=generation_options_for_mode(mode),
            cancel_checker=cancel_checker,
        )
        source_file = os.path.basename(file_path)
        if mode == SEMICONDUCTOR_IP_MODE:
            result = write_semiconductor_ip_outputs(
                source_file=source_file,
                content=content,
                retrieved_docs=retrieved_docs,
                data=validate_semiconductor_ip_result(data),
                output_dir=selected_output_dir,
            )
        else:
            result = write_meeting_outputs(
                source_file=source_file,
                content=content,
                retrieved_docs=retrieved_docs,
                data=data,
                output_dir=selected_output_dir,
                history_recorder=record_audit_history,
            )
        logging.info(
            "工作流【%s】处理成功！结果已保存至 output/ 目录。",
            source_file,
        )
        return result
    except InterruptedError:
        logging.info("工作流【%s】已取消。", os.path.basename(file_path))
        return ProcessResult(success=False, cancelled=True, mode=mode)
    except Exception as exc:
        logging.exception("文件处理失败: %s", exc)
        logging.error(
            "--- 原始模型输出预览 --- \n%s\n------------------------",
            mask_markdown_text(str(exc)[:500]),
        )
        return ProcessResult(success=False, mode=mode)


def process_file(
    file_path: str,
    collection,
    progress_prefix: str = "",
    mode: str = COMPLIANCE_MODE,
    *,
    output_dir: str | None = None,
) -> bool:
    result = process_file_with_result(
        file_path,
        collection,
        progress_prefix,
        mode=mode,
        output_dir=output_dir,
    )
    return result.success
