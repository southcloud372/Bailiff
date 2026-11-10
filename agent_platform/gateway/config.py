"""网关配置：从环境变量 / .env 读取。

配置按「演化速度」分层，避免一个巨型 Settings：
- ``Settings``：网关治理参数（鉴权、限流、沙箱根目录）——变化最慢；
- ``ModelSettings``：模型层参数（供应商、模型名、预算）——跟随供应商调价/换名；
- ``KnowledgeSettings``：基础设施地址（pgvector / 嵌入端点）——跟随部署环境；
- ``RuntimeSettings``：运行时后端选择（checkpoint / 缓存 / 限流）——决定「同一份代码
  跑在笔记本还是生产」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """网关运行配置。env 前缀 AGENT_，默认值面向本地开发。"""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="AGENT_", extra="ignore")

    # 运行环境：决定「安全检查是硬失败还是警告」。
    # dev 下用示例密钥只告警；staging/prod 下直接判 /readyz 不就绪（拒绝上线）。
    env: Literal["dev", "staging", "prod"] = "dev"

    jwt_secret: str = "dev-secret-change-me"  # noqa: S105 - 本地开发默认值，生产必须覆盖
    jwt_algorithm: str = "HS256"
    token_ttl_seconds: int = 3600

    rate_limit_capacity: int = 100
    rate_limit_refill_per_second: float = 10.0

    fs_root: Path = Path("./data/fs")

    # 只读模式：**不注册**写入类工具（而不是注册后拒绝）。
    # 用途：公网演示、外包/第三方接入、审计期间临时收紧。
    # 「能力不存在」比「能力存在但会拒绝」更难被绕过——没有可绕过的分支。
    read_only: bool = False

    # 允许 Agent 抓取的域名白名单（逗号分隔，支持 *.example.com）。
    # 留空 = http_fetch 工具根本不注册。默认拒绝是刻意的：
    # 一个能任意出网的 Agent，等于把内网暴露给任何能对它说话的人。
    http_allowed_hosts: str = ""


class ModelSettings(BaseSettings):
    """模型路由配置。env 前缀 DEEPSEEK_，与网关配置分离（模型层独立演化）。"""

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="DEEPSEEK_", extra="ignore", populate_by_name=True
    )

    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    # DeepSeek 于 2026-07-24 停止 deepseek-chat / deepseek-reasoner，
    # 当前主力为 deepseek-flash（V4.1-Flash）与 deepseek-v4-pro。
    model: str = "deepseek-flash"

    # 模型后端：auto（有 key 用真模型，无 key 降级到离线演示模型）
    # / deepseek（强制真模型，缺 key 直接报错）/ mock（强制离线，用于 demo 与测试）。
    # 开源项目的关键取舍：默认 auto 让「clone 即跑」，而不是「先填 key」。
    llm_backend: Literal["auto", "deepseek", "mock"] = Field(
        default="auto",
        validation_alias=AliasChoices("AGENT_LLM_BACKEND", "DEEPSEEK_LLM_BACKEND"),
    )

    # 成本限额（token 经济学）：0 = 不限。
    cost_limit: float = 0.0
    token_limit: int = 0


class RuntimeSettings(BaseSettings):
    """运行时后端选择：同一份代码适配「本地零依赖」与「生产多副本」。

    env 前缀 AGENT_。默认值刻意选择**不需要任何外部服务**的组合（SQLite + 内存限流），
    保证 clone 下来就能跑；生产通过环境变量切到 PostgreSQL + Redis。
    """

    model_config = SettingsConfigDict(env_file=".env", env_prefix="AGENT_", extra="ignore")

    # ── checkpoint（LangGraph 图状态持久化）──────────────────────────────
    # memory：进程内，仅测试；sqlite：单机持久化，零依赖；postgres：多副本生产。
    checkpoint_backend: Literal["memory", "sqlite", "postgres"] = "sqlite"
    checkpoint_dir: Path = Path("./data/checkpoints")
    postgres_dsn: str = "postgresql://agent:agent@localhost:5432/agent_platform"

    # ── 限流 ───────────────────────────────────────────────────────────
    # memory：进程内令牌桶（单副本）；redis：跨副本共享配额。
    rate_limit_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/0"

    # ── 语义缓存 ───────────────────────────────────────────────────────
    cache_enabled: bool = False  # 需要嵌入服务；本地默认关闭，避免拖慢首次启动
    cache_threshold: float = 0.95
    cache_ttl_seconds: float = 3600.0
    cache_max_entries: int = 512


class KnowledgeSettings(BaseSettings):
    """知识/记忆基础设施配置。env 前缀 AGENT_（与网关共用，字段互不冲突）。"""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="AGENT_", extra="ignore")

    # 基础设施主机：本机 Docker 用 localhost；Docker 跑在 VMware 虚拟机里时改成虚拟机 IP。
    infra_host: str = "localhost"

    # 嵌入：默认本地 Ollama（OpenAI 兼容端点）。留空则按 infra_host 推导。
    embedding_base_url: str = ""
    embedding_model: str = "bge-m3"
    # 向量维度必须与模型一致（bge-m3 = 1024）。写错会在插入时报维度不匹配。
    embedding_dimensions: int = 1024

    # pgvector 连接串（对齐 docker-compose.yml 里的 db 服务）。留空则按 infra_host 推导。
    vector_dsn: str = ""

    @model_validator(mode="after")
    def _derive_endpoints(self) -> KnowledgeSettings:
        if not self.embedding_base_url:
            self.embedding_base_url = f"http://{self.infra_host}:11434/v1"
        if not self.vector_dsn:
            self.vector_dsn = (
                f"postgresql://agent:agent@{self.infra_host}:5432/agent_platform"
            )
        return self
