import hashlib
import json
from datetime import date
from pathlib import Path

from .schemas import PatentClaim, SyntheticPatent


PATENT_KEYS = {
    "document_id", "title", "abstract", "applicant", "filing_date",
    "classification", "claims", "synthetic",
}
CLAIM_KEYS = {"claim_id", "text"}


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonblank string")
    return value.strip()


def load_corpus(path: Path | str) -> list[SyntheticPatent]:
    rows = []
    seen = set()
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON at line {line_number}") from exc
        unknown = set(raw) - PATENT_KEYS
        missing = PATENT_KEYS - set(raw)
        if unknown or missing:
            raise ValueError(f"unknown or missing fields at line {line_number}: {sorted(unknown | missing)}")
        document_id = _text(raw["document_id"], "document_id")
        if document_id in seen:
            raise ValueError(f"duplicate document_id: {document_id}")
        seen.add(document_id)
        try:
            date.fromisoformat(_text(raw["filing_date"], "filing_date"))
        except ValueError as exc:
            raise ValueError("filing_date must be an ISO date") from exc
        if not isinstance(raw["classification"], list):
            raise ValueError("classification must be a list")
        classifications = tuple(_text(item, "classification") for item in raw["classification"])
        if not isinstance(raw["claims"], list) or not raw["claims"]:
            raise ValueError("claims must be a nonempty list")
        claims = []
        for claim in raw["claims"]:
            if not isinstance(claim, dict) or set(claim) != CLAIM_KEYS:
                raise ValueError("claim has unknown or missing fields")
            claims.append(PatentClaim(_text(claim["claim_id"], "claim_id"), _text(claim["text"], "claim text")))
        if raw["synthetic"] is not True:
            raise ValueError("synthetic must be true")
        rows.append(SyntheticPatent(
            document_id=document_id,
            title=_text(raw["title"], "title"),
            abstract=_text(raw["abstract"], "abstract"),
            applicant=_text(raw["applicant"], "applicant"),
            filing_date=raw["filing_date"],
            classification=classifications,
            claims=tuple(claims),
            synthetic=True,
        ))
    return rows


def corpus_version(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
