"""本地检索栈的测试：零依赖 RAG 是否真的能用。

重点不是「向量对不对」，而是三件会上线翻车的事：
1. 嵌入必须跨进程稳定（用 hash 而不是 Python 内置 hash，后者每个进程加盐不同）；
2. 过滤必须先于排序（否则按用户分域检索会漏召回）；
3. 清空与计数必须准（否则重复灌库会让同一段落出现多份）。
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from agent_platform.gateway.config import KnowledgeSettings
from agent_platform.knowledge.chunking import Chunk
from agent_platform.knowledge.factory import build_knowledge_stack
from agent_platform.knowledge.local import HashingEmbedder, SqliteVectorStore
from agent_platform.knowledge.rag import RAGPipeline
from agent_platform.knowledge.store import VectorRecord


def _record(rid: str, text: str, vector: list[float], **meta: str) -> VectorRecord:
    return VectorRecord(id=rid, text=text, embedding=vector, metadata=meta)


@pytest.mark.asyncio
async def test_embedding_is_deterministic_and_normalized() -> None:
    # 新建两个实例，模拟两个进程：结果必须一致（不能用内置 hash，它每进程加盐）
    first = (await HashingEmbedder(dimensions=128).embed(["远程办公怎么申请"]))[0]
    second = (await HashingEmbedder(dimensions=128).embed(["远程办公怎么申请"]))[0]
    assert first == second
    assert math.isclose(math.sqrt(sum(v * v for v in first)), 1.0, rel_tol=1e-6)


@pytest.mark.asyncio
async def test_unrelated_text_gets_different_vector() -> None:
    embedder = HashingEmbedder(dimensions=128)
    query, unrelated = await embedder.embed(["远程办公怎么申请", "服务器机房温度告警"])
    assert query != unrelated


@pytest.mark.asyncio
async def test_chinese_query_matches_right_document() -> None:
    """哈希嵌入靠字面重叠，至少要保证同主题文档排在无关文档前面。"""
    embedder = HashingEmbedder(dimensions=512)
    texts = [
        "远程办公每周最多两天，需要提前一天在系统提交申请。",
        "差旅报销需保留发票，住宿标准按城市分级。",
        "服务器机房温度超过 28 度需要告警。",
    ]
    vectors = await embedder.embed(texts)
    query = (await embedder.embed(["远程办公申请流程"]))[0]

    def score(v: list[float]) -> float:
        return sum(a * b for a, b in zip(query, v, strict=True))

    ranked = sorted(range(len(texts)), key=lambda i: score(vectors[i]), reverse=True)
    assert ranked[0] == 0


@pytest.mark.asyncio
async def test_store_search_and_top_k(tmp_path: Path) -> None:
    store = SqliteVectorStore(tmp_path / "v.sqlite")
    await store.add(
        [
            _record("a", "远程办公", [1.0, 0.0], kind="policy"),
            _record("b", "报销标准", [0.0, 1.0], kind="policy"),
        ]
    )
    hits = await store.search([1.0, 0.0], top_k=1)
    assert [h.id for h in hits] == ["a"]
    assert hits[0].score > 0.99
    await store.aclose()


@pytest.mark.asyncio
async def test_store_filter_is_applied_before_ranking(tmp_path: Path) -> None:
    """过滤必须先于截断，否则「先取 top_k 再过滤」会让召回直接塌掉。"""
    store = SqliteVectorStore(tmp_path / "v.sqlite")
    await store.add(
        [
            _record("other-user", "远程办公", [1.0, 0.0], user_id="bob"),
            _record("mine", "远程办公", [0.9, 0.1], user_id="alice"),
        ]
    )
    hits = await store.search([1.0, 0.0], top_k=1, filter={"user_id": "alice"})
    assert [h.id for h in hits] == ["mine"]


@pytest.mark.asyncio
async def test_store_upsert_and_clear(tmp_path: Path) -> None:
    store = SqliteVectorStore(tmp_path / "v.sqlite")
    await store.add([_record("a", "v1", [1.0, 0.0])])
    await store.add([_record("a", "v2", [1.0, 0.0])])  # 同 id 覆盖，不应变成两条
    assert await store.count() == 1
    hits = await store.search([1.0, 0.0], top_k=5)
    assert hits[0].text == "v2"
    await store.clear()
    assert await store.count() == 0


def test_factory_defaults_to_local_backend() -> None:
    stack = build_knowledge_stack(KnowledgeSettings())
    assert stack.backend == "local"
    assert stack.is_local


@pytest.mark.asyncio
async def test_ingest_then_search_end_to_end(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "remote.md").write_text(
        "# 远程办公\n\n每周最多 2 天远程办公，需提前一天提交申请。", encoding="utf-8"
    )
    (corpus / "travel.md").write_text(
        "# 差旅\n\n出差住宿标准按城市分级，需保留发票。", encoding="utf-8"
    )

    stack = build_knowledge_stack(
        KnowledgeSettings(local_db_path=tmp_path / "kb.sqlite")  # type: ignore[call-arg]
    )
    rag = RAGPipeline(stack.store, stack.embedder)
    added = await rag.ingest(corpus)
    assert added >= 2

    hits = await rag.search("远程办公如何申请", top_k=1)
    assert hits
    assert "remote.md" in str(hits[0].metadata.get("source"))


def test_chunk_metadata_is_preserved() -> None:
    chunk = Chunk(id="x#0", text="t", source="x.md", index=0, metadata={"tenant": "acme"})
    assert chunk.metadata["tenant"] == "acme"
