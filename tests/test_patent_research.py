import json
from pathlib import Path

import pytest

from agent_runtime.evidence import source_sha256
from agent_runtime.models import SessionStatus
from agent_runtime.runtime import AgentRuntime
from agent_runtime.session_store import SessionStore
from agent_runtime.tools import ToolRegistry
from capabilities.patent_research.corpus import corpus_version, load_corpus
from capabilities.patent_research.playbook import (
    PatentPlaybookPlanner,
    build_patent_capability,
)
from capabilities.patent_research.search import (
    PatentHit,
    build_semantic_index,
    keyword_search,
    merge_ranked_hits,
    semantic_search,
)
from capabilities.patent_research.tools import register_patent_tools


CORPUS = Path("capabilities/patent_research/corpus/synthetic_sic_patents.jsonl")
PATENT_EVAL_CASES = json.loads(
    Path("tests/fixtures/agent_eval/patent_cases.json").read_text(encoding="utf-8")
)


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


BRIEF = Path("examples/agent_demo/sic_trench_product_brief.txt").read_text(
    encoding="utf-8"
)


def feature_result():
    return {
        "technical_features": [{
            "feature_id": "F1",
            "feature": "沟槽底部接地屏蔽区",
            "synonyms": ["底部屏蔽电极", "source-connected shield"],
            "evidence_quote": "沟槽底部设置与源极相连、保持接地电位的屏蔽区，用于降低栅介质承受的电场并改善长期可靠性",
        }],
        "keyword_queries": [["沟槽", "底部屏蔽区"], ["栅介质电场", "可靠性"]],
        "semantic_queries": ["降低 SiC 沟槽 MOSFET 栅氧电场的屏蔽结构"],
    }


def make_patent_runtime(tmp_path: Path, *, empty=False):
    corpus = load_corpus(CORPUS)
    registry = ToolRegistry()
    register_patent_tools(registry)
    services = {
        "source_text": BRIEF,
        "source_id": "sic_trench_product_brief.txt",
        "patent_state": {},
        "patent_corpus": corpus,
        "patent_feature_model": lambda system, prompt: feature_result(),
        "patent_keyword_search": (
            (lambda patents, terms, limit: []) if empty else keyword_search
        ),
        "patent_semantic_search": (
            (lambda queries, limit: [])
            if empty
            else lambda queries, limit: [
                PatentHit("SYN-SIC-009", 0.9, frozenset({"semantic"}), semantic_rank=1, semantic_locator="claim:C1"),
                PatentHit("SYN-SIC-001", 0.8, frozenset({"semantic"}), semantic_rank=2, semantic_locator="claim:C1"),
            ]
        ),
    }
    store = SessionStore(tmp_path / "sessions")
    runtime = AgentRuntime(
        store=store,
        registry=registry,
        planner=PatentPlaybookPlanner(),
        capability=build_patent_capability(),
        services=services,
    )
    session = runtime.create_session(
        goal="在合成语料中检索并比较沟槽底部屏蔽结构",
        source_name="sic_trench_product_brief.txt",
        source_sha256=source_sha256(BRIEF),
        model_name="demo",
        knowledge_version=corpus_version(CORPUS),
    )
    runtime.approve(session.session_id)
    return runtime, store, session.session_id


def test_patent_golden_path_produces_verified_synthetic_artifacts(tmp_path: Path):
    runtime, store, session_id = make_patent_runtime(tmp_path)
    session = runtime.run_until_pause(session_id)

    assert session.status is SessionStatus.COMPLETED
    assert session.model_calls <= 5
    assert session.tool_calls <= 14
    assert session.query_rounds <= 2
    artifact_dir = store.artifact_dir(session_id)
    report_path = artifact_dir / "patent_research_report.md"
    retrieval_path = artifact_dir / "patent_retrieval_results.csv"
    chart_path = artifact_dir / "claim_chart.csv"
    assert report_path.exists() and retrieval_path.exists() and chart_path.exists()
    retrieval = retrieval_path.read_text(encoding="utf-8-sig")
    assert "SYN-SIC-001" in retrieval and "SYN-SIC-009" in retrieval
    chart = chart_path.read_text(encoding="utf-8-sig")
    assert "document_id" in chart and "claim_id" in chart and "evidence_ids" in chart
    report = report_path.read_text(encoding="utf-8")
    assert "合成语料" in report and "不构成法律意见" in report

    patents = {patent.document_id: patent for patent in load_corpus(CORPUS)}
    source_texts = {BRIEF}
    for patent in patents.values():
        source_texts.add(patent.abstract)
        source_texts.update(claim.text for claim in patent.claims)
    evidence = json.loads(
        (artifact_dir.parent / "evidence.json").read_text(encoding="utf-8")
    )
    assert evidence
    assert all(any(item["quote"] in text for text in source_texts) for item in evidence)


def test_patent_no_result_stops_incomplete_without_fabrication(tmp_path: Path):
    runtime, store, session_id = make_patent_runtime(tmp_path, empty=True)
    session = runtime.run_until_pause(session_id)

    assert session.status is SessionStatus.INCOMPLETE
    assert session.query_rounds == 2
    assert session.tool_calls <= 14
    assert not (store.artifact_dir(session_id) / "patent_research_report.md").exists()
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (store.artifact_dir(session_id).parent).glob("*.json*")
    )
    assert "SYN-SIC-999" not in persisted


@pytest.mark.parametrize("case", PATENT_EVAL_CASES, ids=lambda case: case["case_id"])
def test_patent_evaluation_contract(tmp_path: Path, case):
    patents = load_corpus(CORPUS)
    registry = ToolRegistry()
    register_patent_tools(registry)
    feature_result = {
        "technical_features": [{
            "feature_id": "F1",
            "feature": case["feature"],
            "synonyms": [],
            "evidence_quote": case["feature"],
        }],
        "keyword_queries": case["keyword_queries"],
        "semantic_queries": [case["feature"]],
    }
    semantic_hits = [
        PatentHit(document_id, 1.0 / rank, frozenset({"semantic"}), semantic_rank=rank, semantic_locator="claim:C1")
        for rank, document_id in enumerate(case["semantic_ids"], 1)
    ]
    services = {
        "source_text": case["source_text"],
        "source_id": f"{case['case_id']}.txt",
        "patent_state": {},
        "patent_corpus": patents,
        "patent_feature_model": lambda system, prompt: feature_result,
        "patent_keyword_search": keyword_search,
        "patent_semantic_search": lambda queries, limit: semantic_hits,
    }
    store = SessionStore(tmp_path / "sessions")
    runtime = AgentRuntime(
        store=store,
        registry=registry,
        planner=PatentPlaybookPlanner(),
        capability=build_patent_capability(),
        services=services,
    )
    session = runtime.create_session(
        goal="固定专利评估",
        source_name=f"{case['case_id']}.txt",
        source_sha256=source_sha256(case["source_text"]),
        model_name="fake",
        knowledge_version=corpus_version(CORPUS),
    )
    runtime.approve(session.session_id)
    session = runtime.run_until_pause(session.session_id)

    assert session.status.value == case["expected_status"]
    assert session.model_calls <= case["max_model_calls"]
    assert session.tool_calls <= case["max_tool_calls"]
    assert session.query_rounds <= case["max_query_rounds"]
    observations = [
        item for item in session.observations
        if item.get("tool_name") == "patent.semantic_search"
    ]
    candidates = observations[-1]["data"]["candidates"]
    for expected in case["expected_candidate_ids"]:
        assert expected in candidates[:2]
    if case["requires_evidence"]:
        evidence = store.load_evidence(session.session_id)
        assert evidence
        source_texts = {case["source_text"]}
        for patent in patents:
            source_texts.add(patent.abstract)
            source_texts.update(claim.text for claim in patent.claims)
        assert all(any(item.quote in text for text in source_texts) for item in evidence)
    if session.status is SessionStatus.COMPLETED:
        report = (store.artifact_dir(session.session_id) / "patent_research_report.md").read_text(encoding="utf-8")
        assert all(
            heading in report
            for heading in ("目标与范围", "排名候选", "权利要求对比", "证据索引", "不构成法律意见")
        )
    else:
        assert not (store.artifact_dir(session.session_id) / "patent_research_report.md").exists()
    assert all(
        item.get("tool_name", "").startswith("patent.")
        for item in session.observations if item.get("tool_name")
    )
