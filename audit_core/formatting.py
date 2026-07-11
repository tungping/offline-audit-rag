import re
from typing import Any

import pandas as pd

import audit_rules


def extract_json_object(response_text):
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
    end_idx = -1
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
                end_idx = idx
                break

    if end_idx == -1:
        raise ValueError("模型输出中的 JSON 对象不完整")

    json_str = text[start : end_idx + 1].strip()
    json_str = re.sub(r",\s*\]", "]", json_str)
    json_str = re.sub(r",\s*\}", "}", json_str)
    return json_str


def markdown_table_cell(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def markdown_quote_block(text):
    return "\n".join(f"> {line}" if line else ">" for line in text.strip().splitlines())


def mask_markdown_text(value):
    return audit_rules.mask_sensitive_evidence(str(value))


def mask_output_basename(base_name):
    return audit_rules.mask_sensitive_evidence(base_name).replace("*", "x")


def mask_dataframe_text_columns(df):
    masked_df = df.copy()
    for column in masked_df.select_dtypes(include=["object", "string"]).columns:
        masked_df[column] = masked_df[column].map(
            lambda value: audit_rules.mask_sensitive_evidence(value)
            if isinstance(value, str)
            else value
        )
    return masked_df


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y", "是"}


def _choice(value: Any, allowed: set[str], default: str) -> str:
    normalized = _clean_text(value).lower()
    for item in allowed:
        if normalized == item.lower():
            return item
    return default
