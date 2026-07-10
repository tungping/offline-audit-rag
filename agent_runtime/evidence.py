import hashlib
import uuid

from .models import Evidence


def source_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify_quote(
    *,
    source_type: str,
    source_id: str,
    locator: str,
    source_text: str,
    quote: str,
) -> Evidence:
    if not quote:
        raise ValueError("quote must not be empty")
    if quote not in source_text:
        raise ValueError("quote not found in source text")
    return Evidence(
        evidence_id=uuid.uuid4().hex,
        source_type=source_type,
        source_id=source_id,
        locator=locator,
        quote=quote,
        source_sha256=source_sha256(source_text),
    )
