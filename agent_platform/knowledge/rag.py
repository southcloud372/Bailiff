"""RAG 管道：加载 → 切分 → 向量化 → 存储 → 检索 → 引用溯源。

把知识库文档灌进向量库，并把「检索 + 引用格式化」暴露成一个工具（search_knowledge），
Agent 可据此做「先检索后回答」的 grounded 生成。
"""

from __future__ import annotations

from pathlib import Path

from agent_platform.knowledge.chunking import load_documents
from agent_platform.knowledge.embedder import Embedder
from agent_platform.knowledge.retriever import retrieve
from agent_platform.knowledge.store import ScoredRecord, VectorRecord, VectorStore

KIND = "knowledge"


class RAGPipeline:
    """知识库 RAG 管道。"""

    def __init__(self, store: VectorStore, embedder: Embedder, *, top_k: int = 4) -> None:
        self._store = store
        self._embedder = embedder
        self._top_k = top_k

    async def ingest(self, docs_dir: str | Path) -> int:
        """加载并向量化知识库文档，返回写入的片段数。"""
        chunks = load_documents(docs_dir)
        embeddings = await self._embedder.embed([c.text for c in chunks])
        records = [
            VectorRecord(
                id=c.id,
                text=c.text,
                embedding=emb,
                metadata={"kind": KIND, "source": c.source, **c.metadata},
            )
            for c, emb in zip(chunks, embeddings, strict=True)
        ]
        await self._store.add(records)
        return len(records)

    async def search(self, query: str, *, top_k: int | None = None) -> list[ScoredRecord]:
        """检索知识库，返回带来源的命中片段。"""
        return await retrieve(
            self._store,
            self._embedder,
            query,
            top_k=top_k or self._top_k,
            filter={"kind": KIND},
        )

    @staticmethod
    def format_context(hits: list[ScoredRecord]) -> str:
        """把命中片段格式化成可注入提示词的上下文（含来源引用）。"""
        if not hits:
            return "（知识库中未检索到相关内容）"
        parts: list[str] = []
        for hit in hits:
            source = hit.metadata.get("source", hit.id)
            parts.append(f"[来源: {source}]\n{hit.text}")
        return "\n\n---\n\n".join(parts)


def build_grounded_prompt(question: str, context: str) -> str:
    """拼一个带知识库上下文的提示词（供单独走 RAG 而非 ReAct 工具的场景）。"""
    return (
        "你是企业知识助手。请仅依据下面的知识库上下文回答，若上下文不足以回答，"
        "明确说明「知识库中无相关信息」，不要编造。\n\n"
        f"知识库上下文：\n{context}\n\n"
        f"问题：{question}"
    )
