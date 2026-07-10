import json
from pathlib import Path

import pytest

from capabilities.patent_research.corpus import corpus_version, load_corpus
from capabilities.patent_research.search import (
    build_semantic_index,
    keyword_search,
    merge_ranked_hits,
    semantic_search,
)


CORPUS = Path("capabilities/patent_research/corpus/synthetic_sic_patents.jsonl")


def test_synthetic_corpus_is_valid_and_explicitly_synthetic():
    patents = load_corpus(CORPUS)
    assert len(patents) == 10
    assert len({patent.document_id for patent in patents}) == 10
    assert all(patent.synthetic is True for patent in patents)
    assert all(len(patent.claims) == 2 for patent in patents)
    assert len(corpus_version(CORPUS)) == 64


def test_trench_fixture_contains_expected_retrieval_targets():
    patents = {patent.document_id: patent for patent in load_corpus(CORPUS)}
    assert "沟槽底部屏蔽区" in patents["SYN-SIC-001"].claims[0].text
    assert "分裂栅" in patents["SYN-SIC-009"].claims[0].text


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda row: row.update(extra="unknown"), "unknown"),
        (lambda row: row.update(synthetic=False), "synthetic"),
        (lambda row: row.update(filing_date="2024/01/15"), "date"),
        (lambda row: row.update(classification="H01L"), "classification"),
        (lambda row: row.update(title=""), "title"),
    ],
)
def test_corpus_loader_rejects_invalid_records(tmp_path: Path, mutation, message):
    row = json.loads(CORPUS.read_text(encoding="utf-8").splitlines()[0])
    mutation(row)
    path = tmp_path / "invalid.jsonl"
    path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_corpus(path)


@pytest.fixture
def corpus():
    return load_corpus(CORPUS)


def test_keyword_search_ranks_bottom_shield_patent_first(corpus):
    hits = keyword_search(corpus, ["沟槽", "底部屏蔽区", "栅介质电场"], limit=5)
    assert hits[0].document_id == "SYN-SIC-001"
    assert "keyword" in hits[0].retrievers
    assert "claim" in hits[0].matched_fields


def test_rrf_merge_preserves_retriever_provenance():
    merged = merge_ranked_hits(
        keyword_ids=["SYN-SIC-001", "SYN-SIC-009"],
        semantic_ids=["SYN-SIC-009", "SYN-SIC-001", "SYN-SIC-006"],
        limit=5,
    )
    assert {hit.document_id for hit in merged[:2]} == {
        "SYN-SIC-001", "SYN-SIC-009"
    }
    assert merged[0].retrievers == frozenset({"keyword", "semantic"})
    assert merged[0].keyword_rank and merged[0].semantic_rank


class FakeCollection:
    def __init__(self):
        self.added = None

    def add(self, **kwargs):
        self.added = kwargs

    def query(self, **kwargs):
        return {
            "ids": [["SYN-SIC-009:claim:C1", "SYN-SIC-001:claim:C1"]],
            "documents": [["分裂栅与源极连接屏蔽电极", "沟槽底部屏蔽区降低栅介质电场"]],
            "metadatas": [[
                {"document_id": "SYN-SIC-009", "section": "claim", "claim_id": "C1"},
                {"document_id": "SYN-SIC-001", "section": "claim", "claim_id": "C1"},
            ]],
            "distances": [[0.2, 0.3]],
        }


def test_semantic_index_metadata_and_search_are_injectable(corpus):
    collection = FakeCollection()
    embed = lambda texts: [[float(index)] for index, _ in enumerate(texts)]
    build_semantic_index(collection, corpus[:1], embed)
    assert collection.added["ids"] == [
        "SYN-SIC-001:abstract",
        "SYN-SIC-001:claim:C1",
        "SYN-SIC-001:claim:C2",
    ]
    hits = semantic_search(collection, ["屏蔽结构"], embed, limit=5)
    assert [hit.document_id for hit in hits] == ["SYN-SIC-009", "SYN-SIC-001"]
    assert hits[0].semantic_locator == "claim:C1"
    assert hits[0].retrievers == frozenset({"semantic"})
