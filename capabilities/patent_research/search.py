import re
from dataclasses import dataclass, replace
from typing import Callable, Iterable

from audit_core.config import RELEVANCE_THRESHOLD
from audit_core.knowledge_base import is_relevant_distance

from .schemas import SyntheticPatent


@dataclass(frozen=True)
class PatentHit:
    document_id: str
    score: float
    retrievers: frozenset[str]
    keyword_rank: int | None = None
    semantic_rank: int | None = None
    matched_terms: tuple[str, ...] = ()
    matched_fields: tuple[str, ...] = ()
    semantic_locator: str = ""
    semantic_quote: str = ""


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def keyword_search(
    patents: Iterable[SyntheticPatent], phrases: Iterable[str], limit: int = 5
) -> list[PatentHit]:
    terms = tuple(dict.fromkeys(_normalize(term) for term in phrases if _normalize(term)))
    hits = []
    for patent in patents:
        score = 0.0
        matched_terms = set()
        matched_fields = set()
        title = _normalize(patent.title)
        abstract = _normalize(patent.abstract)
        claims = [_normalize(claim.text) for claim in patent.claims]
        classifications = {_normalize(item) for item in patent.classification}
        for term in terms:
            term_score = 0
            if term in title:
                term_score += 3
                matched_fields.add("title")
            if term in abstract:
                term_score += 2
                matched_fields.add("abstract")
            if any(term in claim for claim in claims):
                term_score += 4
                matched_fields.add("claim")
            if term in classifications:
                term_score += 2
                matched_fields.add("classification")
            if term_score:
                score += term_score
                matched_terms.add(term)
        if score:
            hits.append(PatentHit(
                document_id=patent.document_id,
                score=score,
                retrievers=frozenset({"keyword"}),
                matched_terms=tuple(sorted(matched_terms)),
                matched_fields=tuple(sorted(matched_fields)),
            ))
    hits.sort(key=lambda item: (-item.score, item.document_id))
    return [replace(hit, keyword_rank=index) for index, hit in enumerate(hits[:limit], 1)]


def build_semantic_index(
    collection,
    patents: Iterable[SyntheticPatent],
    embed: Callable[[list[str]], list[list[float]]],
) -> None:
    ids, documents, metadatas = [], [], []
    for patent in patents:
        ids.append(f"{patent.document_id}:abstract")
        documents.append(patent.abstract)
        metadatas.append({"document_id": patent.document_id, "section": "abstract", "claim_id": ""})
        for claim in patent.claims:
            ids.append(f"{patent.document_id}:claim:{claim.claim_id}")
            documents.append(claim.text)
            metadatas.append({"document_id": patent.document_id, "section": "claim", "claim_id": claim.claim_id})
    if documents:
        collection.add(ids=ids, documents=documents, metadatas=metadatas, embeddings=embed(documents))


def semantic_search(
    collection,
    queries: Iterable[str],
    embed: Callable[[list[str]], list[list[float]]],
    limit: int = 5,
    threshold: float = RELEVANCE_THRESHOLD,
) -> list[PatentHit]:
    query_list = [query for query in queries if query.strip()]
    if not query_list:
        return []
    best = {}
    for vector in embed(query_list):
        result = collection.query(
            query_embeddings=[vector], n_results=limit,
            include=["documents", "metadatas", "distances"],
        )
        documents = result.get("documents", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0]
        for document, metadata, distance in zip(documents, metadatas, distances):
            if not is_relevant_distance(distance, threshold):
                continue
            document_id = metadata["document_id"]
            current = best.get(document_id)
            if current is None or distance < current[0]:
                locator = metadata["section"]
                if metadata.get("claim_id"):
                    locator += f":{metadata['claim_id']}"
                best[document_id] = (float(distance), locator, document)
    ordered = sorted(best.items(), key=lambda item: (item[1][0], item[0]))[:limit]
    return [PatentHit(
        document_id=document_id,
        score=1.0 / (1.0 + value[0]),
        retrievers=frozenset({"semantic"}),
        semantic_rank=rank,
        semantic_locator=value[1],
        semantic_quote=value[2],
    ) for rank, (document_id, value) in enumerate(ordered, 1)]


def merge_ranked_hits(
    keyword_ids: Iterable[str], semantic_ids: Iterable[str], limit: int = 5
) -> list[PatentHit]:
    merged = {}
    for retriever, ids in (("keyword", keyword_ids), ("semantic", semantic_ids)):
        for rank, document_id in enumerate(ids, 1):
            entry = merged.setdefault(document_id, {"score": 0.0, "retrievers": set(), "keyword_rank": None, "semantic_rank": None})
            entry["score"] += 1.0 / (60 + rank)
            entry["retrievers"].add(retriever)
            entry[f"{retriever}_rank"] = rank
    hits = [PatentHit(
        document_id=document_id,
        score=value["score"],
        retrievers=frozenset(value["retrievers"]),
        keyword_rank=value["keyword_rank"],
        semantic_rank=value["semantic_rank"],
    ) for document_id, value in merged.items()]
    hits.sort(key=lambda item: (-item.score, item.document_id))
    return hits[:limit]
