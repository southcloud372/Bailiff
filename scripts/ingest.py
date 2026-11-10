"""把文档灌进检索库。

    uv run python scripts/ingest.py                      # 默认灌 examples/knowledge
    uv run python scripts/ingest.py --dir docs --dir examples/knowledge
    uv run python scripts/ingest.py --reset              # 清空后重灌

默认写入本地 SQLite（data/knowledge.sqlite），不依赖任何外部服务。
配了 AGENT_RAG_BACKEND=postgres 时写入 pgvector。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from agent_platform.gateway.config import KnowledgeSettings
from agent_platform.knowledge.factory import build_knowledge_stack
from agent_platform.knowledge.rag import RAGPipeline


async def _run(dirs: list[Path], reset: bool) -> int:
    settings = KnowledgeSettings()
    stack = build_knowledge_stack(settings)
    rag = RAGPipeline(stack.store, stack.embedder)

    if reset:
        await stack.store.clear()
        print("已清空旧数据")

    total = 0
    for directory in dirs:
        if not directory.is_dir():
            print(f"跳过（不是目录）: {directory}", file=sys.stderr)
            continue
        count = await rag.ingest(directory)
        total += count
        print(f"  {directory} -> {count} 个片段")

    print(f"后端 {stack.backend}，本次写入 {total} 个片段")
    return 0 if total else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="把 markdown/txt 文档灌进检索库")
    parser.add_argument(
        "--dir",
        action="append",
        type=Path,
        help="语料目录，可重复；默认 examples/knowledge",
    )
    parser.add_argument("--reset", action="store_true", help="先清空再灌")
    args = parser.parse_args()

    dirs = args.dir or [Path("examples/knowledge")]
    return asyncio.run(_run(dirs, args.reset))


if __name__ == "__main__":
    raise SystemExit(main())
