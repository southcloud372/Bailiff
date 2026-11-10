"""嵌入层：把文本转成向量。

默认用本地 Ollama 的 OpenAI 兼容端点（无额外付费 key、可自托管），模型可配
（默认 ``bge-m3``，中文/多语质量较好）。``FakeEmbedder`` 提供确定性哈希嵌入，
供单元测试与「无 Ollama 时的降级演示」使用。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from typing import Protocol

import httpx


class Embedder(Protocol):
    """嵌入能力的最小接口：一批文本 → 一批等维向量。"""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class OllamaEmbedder:
    """通过 Ollama 的 OpenAI 兼容 ``/v1/embeddings`` 端点做嵌入。"""

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434/v1",
        model: str = "bge-m3",
        timeout: float = 120.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        # Ollama 忽略 api_key，但 OpenAI 兼容端点要求 Authorization 头非空。
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._base_url}/embeddings",
                headers={"Authorization": "Bearer ollama"},
                json={"model": self._model, "input": list(texts)},
            )
            resp.raise_for_status()
            data = resp.json()["data"]
            # 按 index 排序，保证返回顺序与输入一致
            data = sorted(data, key=lambda item: item["index"])
            return [item["embedding"] for item in data]


class FakeEmbedder:
    """确定性哈希嵌入：字符 3-gram 散列到固定维度，共享词元越多越相似。

    无需外部服务，供测试与降级演示。相似度是「词面重叠」的近似，不等价于语义嵌入。
    """

    def __init__(self, dim: int = 256) -> None:
        self._dim = dim

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        normalized = text.lower()
        for i in range(len(normalized) - 2):
            gram = normalized[i : i + 3].encode("utf-8")
            idx = int.from_bytes(hashlib.md5(gram).digest()[:4], "big") % self._dim  # noqa: S324 - 确定性哈希，非加密
            vec[idx] += 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]
