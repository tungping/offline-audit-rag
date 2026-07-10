import json
import os
from collections.abc import Callable, Iterable
from typing import Any

import ollama

from .config import (
    ARCHIVE,
    AUDIT_MODEL,
    CONFIG_DIR,
    EMBED_MODEL,
    FAILED,
    INBOX,
    OUTPUT,
    SESSIONS_DIR,
    SEMICONDUCTOR_IP_CONFIG_DIR,
    VECTOR_STORE_DIR,
)
from .formatting import extract_json_object, mask_markdown_text


def generate_json_stream(
    *,
    model: str,
    system: str,
    prompt: str,
    options: dict[str, int | float],
    cancel_checker: Callable[[], bool] | None = None,
    generate: Callable[..., Iterable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    chunks: list[str] = []
    generator = generate or ollama.generate
    stream = generator(
        model=model,
        system=system,
        prompt=prompt,
        options=options,
        stream=True,
    )
    for chunk in stream:
        if cancel_checker and cancel_checker():
            raise InterruptedError("generation cancelled")
        chunks.append(str(chunk.get("response", "")))
    raw = "".join(chunks)
    try:
        return json.loads(extract_json_object(raw))
    except Exception as exc:
        raise ValueError(
            f"模型输出无法解析: {mask_markdown_text(raw[:500])}"
        ) from exc


def check_environment():
    directories = [
        INBOX,
        OUTPUT,
        ARCHIVE,
        FAILED,
        CONFIG_DIR,
        SEMICONDUCTOR_IP_CONFIG_DIR,
        VECTOR_STORE_DIR,
        SESSIONS_DIR,
    ]
    for directory in directories:
        os.makedirs(directory, exist_ok=True)


def check_ollama_status() -> dict[str, Any]:
    try:
        client = ollama.Client()
        models_info = client.list()
        models = []
        for model_info in models_info.get("models", []):
            name = (
                getattr(model_info, "model", None)
                or (
                    model_info.get("model")
                    if isinstance(model_info, dict)
                    else None
                )
                or str(model_info)
            )
            models.append(name)

        audit_model_ok = AUDIT_MODEL in models or any(
            model.startswith(AUDIT_MODEL + ":") for model in models
        )
        embed_model_ok = EMBED_MODEL in models or any(
            model.startswith(EMBED_MODEL + ":") for model in models
        )
        return {
            "connected": True,
            "audit_model_ok": audit_model_ok,
            "embed_model_ok": embed_model_ok,
            "models": models,
            "error": "",
        }
    except Exception as exc:
        return {
            "connected": False,
            "audit_model_ok": False,
            "embed_model_ok": False,
            "models": [],
            "error": str(exc),
        }
