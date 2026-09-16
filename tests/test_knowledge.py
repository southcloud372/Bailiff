"""知识库 RAG 管道单元测试（FakeEmbedder + InMemoryVectorStore，不依赖 Docker）。"""

import pytest

from agent_platform.knowledge.chunking import chunk_text, load_documents
from agent_platform.knowledge.embedder import FakeEmbedder
from agent_platform.knowledge.rag import RAGPipeline, build_grounded_prompt
from agent_platform.knowledge.retriever import _lexical_overlap, retrieve
from agent_platform.knowledge.store import InMemoryVectorStore, VectorRecord


def test_chunk_text_splits_on_paragraph_boundaries() -> None:
    text = "\n\n".join(["段落一", "段落二", "段落三"])
    chunks = chunk_text(text, source="a.md", chunk_size=3)
    assert len(chunks) >= 1
    assert chunks[0].id == "a.md#0"
    assert all(c.source == "a.md" for c in chunks)


def test_chunk_text_empty_returns_no_chunks() -> None:
    assert chunk_text("", source="a.md") == []


@pytest.mark.asyncio
async def test_fake_embedder_is_deterministic_and_normalized() -> None:
    emb = FakeEmbedder(dim=64)
    v1 = (await emb.embed(["报销标准"]))[0]
    v2 = (await emb.embed(["报销标准"]))[0]
    assert len(v1) == 64
    assert v1 == v2
    # 归一化：向量模长 ≈ 1
    norm = sum(x * x for x in v1) ** 0.5
    assert abs(norm - 1.0) < 1e-6


def test_lexical_overlap_ranges() -> None:
    assert _lexical_overlap("abc", "abc") == 1.0
    assert _lexical_overlap("abc", "xyz") == 0.0
    assert _lexical_overlap("", "abc") == 0.0


@pytest.mark.asyncio
async def test_in_memory_store_cosine_search_and_filter() -> None:
    store = InMemoryVectorStore()
    await store.add(
        [
            VectorRecord(id="a", text="报销", embedding=[1.0, 0.0], metadata={"kind": "knowledge"}),
            VectorRecord(id="b", text="请假", embedding=[0.0, 1.0], metadata={"kind": "memory"}),
        ]
    )
    hits = await store.search([1.0, 0.0], top_k=1)
    assert hits[0].id == "a"
    # filter 只保留匹配项
    only_memory = await store.search([1.0, 0.0], top_k=10, filter={"kind": "memory"})
    assert [h.id for h in only_memory] == ["b"]


@pytest.mark.asyncio
async def test_rag_ingest_and_search(tmp_path) -> None:
    (tmp_path / "hr.md").write_text("年假 5 天，报销住宿标准 600 元", encoding="utf-8")
    store = InMemoryVectorStore()
    rag = RAGPipeline(store, FakeEmbedder(dim=64))
    count = await rag.ingest(tmp_path)
    assert count >= 1

    hits = await rag.search("报销住宿标准是多少")
    assert hits, "应至少召回一个片段"
    assert "报销" in hits[0].text or "600" in hits[0].text
    # 命中带来源引用
    assert "[来源:" in rag.format_context(hits)


@pytest.mark.asyncio
async def test_retrieve_reranks_and_truncates() -> None:
    store = InMemoryVectorStore()
    await store.add(
        [
            VectorRecord(id="x", text="员工离职流程", embedding=[1.0, 0.0], metadata={}),
            VectorRecord(id="y", text="员工离职流程和交接", embedding=[1.0, 0.1], metadata={}),
        ]
    )
    hits = await retrieve(store, FakeEmbedder(dim=64), "员工离职流程", top_k=1)
    assert len(hits) == 1


def test_load_documents_skips_unsupported(tmp_path) -> None:
    (tmp_path / "a.md").write_text("内容一", encoding="utf-8")
    (tmp_path / "b.txt").write_text("内容二", encoding="utf-8")
    (tmp_path / "c.py").write_text("print(1)", encoding="utf-8")
    chunks = load_documents(tmp_path)
    assert {c.source for c in chunks} == {"a.md", "b.txt"}


def test_build_grounded_prompt_contains_question_and_context() -> None:
    prompt = build_grounded_prompt("年假几天？", "[来源: hr.md]\n年假 5 天")
    assert "年假几天？" in prompt
    assert "[来源: hr.md]" in prompt
