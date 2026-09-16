"""网关配置：从环境变量 / .env 读取。"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """网关运行配置。env 前缀 AGENT_，默认值面向本地开发。"""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="AGENT_", extra="ignore")

    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    token_ttl_seconds: int = 3600

    rate_limit_capacity: int = 100
    rate_limit_refill_per_second: float = 10.0

    fs_root: Path = Path("./data/fs")


class ModelSettings(BaseSettings):
    """模型路由配置。env 前缀 DEEPSEEK_，与网关配置分离（模型层独立演化）。"""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="DEEPSEEK_", extra="ignore")

    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"

    # 成本限额（token 经济学）：0 = 不限。
    cost_limit: float = 0.0
    token_limit: int = 0
