"""文档加载与切分：把知识库文件切成可向量化的片段。

策略：按段落（换行）聚合到约 ``chunk_size`` 字符，段落间保留 ``overlap`` 重叠，
避免把跨段落语义切碎。切分只依赖字符边界，无外部依赖。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_SUPPORTED_SUFFIXES = {".md", ".txt"}


class Chunk(BaseModel):
    """一个可检索片段。"""

    id: str
    text: str
    source: str
    index: int = Field(description="片段在源文档内的序号")
    metadata: dict[str, Any] = Field(default_factory=dict)


def chunk_text(
    text: str,
    *,
    source: str,
    chunk_size: int = 500,
    overlap: int = 80,
    metadata: dict[str, Any] | None = None,
) -> list[Chunk]:
    """把单个文档切成带重叠的片段。"""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    # 重叠不得超过 chunk_size，否则硬切循环会原地打转（尾部拼接重新拼回原文）。
    overlap = max(0, min(overlap, chunk_size - 1))
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if current and len(current) + len(para) + 1 > chunk_size:
            chunks.append(current)
            # 重叠：保留上一段尾部若干字符
            tail = current[-overlap:] if overlap > 0 else ""
            current = tail + para if tail else para
        else:
            current = f"{current}\n{para}" if current else para
        # 单段超长：按 chunk_size 硬切
        while len(current) > chunk_size:
            chunks.append(current[:chunk_size])
            tail = current[chunk_size - overlap : chunk_size] if overlap > 0 else ""
            current = tail + current[chunk_size:]
    if current:
        chunks.append(current)

    meta = metadata or {}
    return [
        Chunk(id=f"{source}#{i}", text=c, source=source, index=i, metadata=dict(meta))
        for i, c in enumerate(chunks)
    ]


def load_documents(docs_dir: str | Path) -> list[Chunk]:
    """加载目录下的 .md / .txt 文件并切分（按文件名排序，结果可复现）。"""
    root = Path(docs_dir)
    if not root.is_dir():
        return []
    chunks: list[Chunk] = []
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in _SUPPORTED_SUFFIXES or not path.is_file():
            continue
        source = str(path.relative_to(root))
        text = path.read_text(encoding="utf-8")
        chunks.extend(chunk_text(text, source=source))
    return chunks
