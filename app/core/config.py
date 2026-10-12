"""应用配置。

所有可变量都走环境变量，配合 .env（本地用，不提交）和 .env.example（模板，提交）。
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="APP_", extra="ignore")

    app_name: str = "门诊预约与智能分诊系统"
    debug: bool = False

    # 默认 SQLite，clone 下来不用装数据库就能跑；生产换成 PostgreSQL 只需改这一行
    database_url: str = "sqlite:///./data/app.db"

    # 分诊生成模式：rule（零依赖）/ local（Ollama）/ api（需要自己的 key）
    triage_mode: str = "rule"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"
    api_base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    api_model: str = "deepseek-chat"

    # 号源并发重试次数（原子扣减失败后的重试上限）
    booking_max_retry: int = 3


@lru_cache
def get_settings() -> Settings:
    return Settings()
