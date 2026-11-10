"""检索基准评测：对比「纯向量」与「混合检索（向量 + 词面重排）」的命中率。

用途：给简历里「混合检索命中率提升」提供可复现的实测数字。

用法：uv run python scripts/benchmark_rag.py [--top-k 3] [--embedding-base-url ...] [--force-fake]

- 默认走 Ollama 的 bge-m3 嵌入（真实语义），端点与模型从 `KnowledgeSettings`
  读取（读 .env 的 `AGENT_INFRA_HOST` / `AGENT_EMBEDDING_*` 自动推导）。
- Ollama 不可达时：加 `--embedding-base-url` 指向可达端点，或 `--force-fake`
  跑「词面近似」基线（仅用于验证脚本可跑通，不代表真实语义检索质量）。

退出码：0 正常；1 无文档/golden；2 嵌入失败。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from agent_platform.gateway.config import KnowledgeSettings
from agent_platform.knowledge.chunking import Chunk, load_documents
from agent_platform.knowledge.embedder import Embedder, FakeEmbedder, OllamaEmbedder
from agent_platform.knowledge.retriever import rerank
from agent_platform.knowledge.store import InMemoryVectorStore, VectorRecord

GOLDEN = Path("agent_platform/evaluation/rag_golden.jsonl")
DEFAULT_DOCS = Path("examples/knowledge")
KIND = "knowledge"


def _load_golden(path: Path) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


def _relevant_ids(chunks: list[Chunk], source: str, answer: str) -> list[str]:
    # 精确命中 answer 子串的片段视为相关；找不到则退化为「源文档级相关」。
    ids = [c.id for c in chunks if c.source == source and answer in c.text]
    return ids or [c.id for c in chunks if c.source == source]


def _metrics(ranked_ids: list[str], relevant_ids: list[str], top_k: int) -> dict[str, float]:
    relevant = set(relevant_ids)
    mrr = 0.0
    for rank, cid in enumerate(ranked_ids, start=1):
        if cid in relevant:
            mrr = 1.0 / rank
            break
    top = ranked_ids[:top_k]
    hit_1 = 1.0 if ranked_ids and ranked_ids[0] in relevant else 0.0
    hit_k = 1.0 if any(c in relevant for c in top) else 0.0
    recall_k = len([c for c in top if c in relevant]) / len(relevant) if relevant else 0.0
    return {"hit_1": hit_1, "hit_k": hit_k, "recall_k": recall_k, "mrr": mrr}


def _mean(rows: list[dict[str, float]], key: str) -> float:
    return sum(r[key] for r in rows) / len(rows) if rows else 0.0


async def _run(
    docs_dir: Path,
    golden_path: Path,
    top_k: int,
    embedding_base_url: str,
    embedding_model: str,
    force_fake: bool,
) -> int:
    chunks = load_documents(docs_dir)
    if not chunks:
        print("[x] 未加载到任何文档片段，请检查 --docs-dir")
        return 1

    embedder: Embedder
    if force_fake:
        embedder = FakeEmbedder()
        print("[!] 使用 FakeEmbedder（词面近似基线，不代表真实语义质量）")
    else:
        embedder = OllamaEmbedder(base_url=embedding_base_url, model=embedding_model)
        print(f"[i] 嵌入端点：{embedding_base_url} / {embedding_model}")

    texts = [c.text for c in chunks]
    try:
        embeddings = await embedder.embed(texts)
    except Exception as exc:
        print(f"[x] 嵌入失败（Ollama 不可达？）：{exc}")
        print("    重跑时加 --embedding-base-url 指向可达端点，或 --force-fake 验证脚本。")
        return 2

    store = InMemoryVectorStore()
    records = [
        VectorRecord(
            id=c.id,
            text=c.text,
            embedding=emb,
            metadata={"kind": KIND, "source": c.source},
        )
        for c, emb in zip(chunks, embeddings, strict=True)
    ]
    await store.add(records)

    golden = _load_golden(golden_path)
    if not golden:
        print("[x] golden 数据集为空")
        return 1

    vec_rows: list[dict[str, float]] = []
    hyb_rows: list[dict[str, float]] = []
    for item in golden:
        query = item["query"]
        relevant = _relevant_ids(chunks, item["source"], item["answer"])
        qvec = (await embedder.embed([query]))[0]

        vec_hits = await store.search(qvec, top_k=top_k, filter={"kind": KIND})
        cand = await store.search(qvec, top_k=max(top_k * 2, top_k), filter={"kind": KIND})
        hyb_hits = rerank(cand, query)[:top_k]

        vec_rows.append(_metrics([h.id for h in vec_hits], relevant, top_k))
        hyb_rows.append(_metrics([h.id for h in hyb_hits], relevant, top_k))

    print(f"\n文档片段 {len(chunks)} 个，golden 用例 {len(golden)} 条，top_k={top_k}")
    print(f"{'指标':<12}{'纯向量':>12}{'混合检索':>12}{'提升':>12}")
    for key, label in [
        ("hit_1", "Hit@1"),
        ("hit_k", f"Hit@{top_k}"),
        ("recall_k", f"Recall@{top_k}"),
        ("mrr", "MRR"),
    ]:
        v = _mean(vec_rows, key)
        h = _mean(hyb_rows, key)
        print(f"{label:<12}{v:>11.1%}{h:>12.1%}{(h - v):>+12.1%}")

    v_recall = _mean(vec_rows, "recall_k")
    h_recall = _mean(hyb_rows, "recall_k")
    print(
        f"\n一句话结论：混合检索 Recall@{top_k} = {h_recall:.1%}，"
        f"纯向量 = {v_recall:.1%}，提升 {h_recall - v_recall:+.1%}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="RAG 检索基准：纯向量 vs 混合检索")
    parser.add_argument("--docs-dir", type=Path, default=DEFAULT_DOCS)
    parser.add_argument("--golden", type=Path, default=GOLDEN)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--embedding-base-url", default="", help="留空则从 KnowledgeSettings 推导")
    parser.add_argument("--embedding-model", default="", help="留空则用默认 bge-m3")
    parser.add_argument("--force-fake", action="store_true", help="用 FakeEmbedder 跑词面近似基线")
    args = parser.parse_args()

    ks = KnowledgeSettings()
    base_url = args.embedding_base_url or ks.embedding_base_url
    model = args.embedding_model or ks.embedding_model

    return asyncio.run(
        _run(args.docs_dir, args.golden, args.top_k, base_url, model, args.force_fake)
    )


if __name__ == "__main__":
    raise SystemExit(main())
