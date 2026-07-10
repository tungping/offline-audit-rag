import json
from pathlib import Path

import pytest

from capabilities.patent_research.corpus import corpus_version, load_corpus


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
