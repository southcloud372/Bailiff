from types import SimpleNamespace

import pytest

from agent_platform.gateway.config import ModelSettings
from agent_platform.router.llm import ModelRouter, ModelRoutingError, build_router


def _resp(
    content: str, *, model: str = "m1", prompt: int = 1, completion: int = 1
) -> SimpleNamespace:
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
        ),
    )


@pytest.mark.asyncio
async def test_complete_returns_result() -> None:
    calls: list[str] = []

    async def fake(**kwargs: object) -> SimpleNamespace:
        calls.append(str(kwargs["model"]))
        return _resp("你好", model="m1", prompt=5, completion=2)

    router = ModelRouter(["m1"], api_key="k", base_url="http://x", completion=fake)
    result = await router.complete([{"role": "user", "content": "hi"}])

    assert result.model == "m1"
    assert result.content == "你好"
    assert result.total_tokens == 7
    assert calls == ["openai/m1"]


@pytest.mark.asyncio
async def test_fallback_when_primary_fails() -> None:
    calls: list[str] = []

    async def fake(**kwargs: object) -> SimpleNamespace:
        calls.append(str(kwargs["model"]))
        if kwargs["model"] == "openai/m1":
            raise RuntimeError("m1 down")
        return _resp("ok", model="m2")

    router = ModelRouter(["m1", "m2"], api_key="k", base_url="http://x", completion=fake)
    result = await router.complete([{"role": "user", "content": "hi"}])

    assert result.model == "m2"
    assert calls == ["openai/m1", "openai/m2"]


@pytest.mark.asyncio
async def test_all_models_fail_raises() -> None:
    async def fake(**kwargs: object) -> SimpleNamespace:
        raise RuntimeError("down")

    router = ModelRouter(["m1"], api_key="k", base_url="http://x", completion=fake)
    with pytest.raises(ModelRoutingError):
        await router.complete([{"role": "user", "content": "hi"}])


def test_build_router_requires_api_key() -> None:
    with pytest.raises(ModelRoutingError):
        build_router(ModelSettings(api_key="", model="deepseek-chat"))


def test_build_router_no_models_raises() -> None:
    with pytest.raises(ModelRoutingError):
        ModelRouter([], api_key="k", base_url="http://x")
