from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EMBEDDING_MODEL_NAME = "Qwen/Qwen3-Embedding-0.6B"
EMBEDDING_WIDTH = 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Enterprise Agent Runtime"
    api_prefix: str = "/api/v1"
    postgres_host: str = ""
    postgres_port: int = 5432
    postgres_db: str = ""
    agent_app_user: str = "agent_app"
    agent_app_password: str = ""
    agent_migrator_user: str = "agent_migrator"
    agent_migrator_password: str = ""
    redis_host: str = ""
    redis_port: int = 6379
    redis_password: str = ""
    enterprise_infra_enabled: bool = False
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = EMBEDDING_MODEL_NAME
    embedding_dimensions: int = EMBEDDING_WIDTH
    embedding_timeout_seconds: float = 30
    embedding_batch_size: int = 32

    @model_validator(mode="after")
    def _check_embedding_settings(self) -> "Settings":
        if self.embedding_dimensions != EMBEDDING_WIDTH:
            raise ValueError("EMBEDDING_DIMENSIONS 必须是 1024")
        if self.embedding_model != EMBEDDING_MODEL_NAME:
            raise ValueError("EMBEDDING_MODEL 必须是 Qwen/Qwen3-Embedding-0.6B")
        if type(self.embedding_batch_size) is not int or not 1 <= self.embedding_batch_size <= 32:
            raise ValueError("EMBEDDING_BATCH_SIZE 必须在 1 到 32 之间")
        timeout = self.embedding_timeout_seconds
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < float(timeout) <= 120:
            raise ValueError("EMBEDDING_TIMEOUT_SECONDS 必须在 0 到 120 秒之间")
        base_url = self.siliconflow_base_url.strip().rstrip("/")
        if not (base_url.startswith("https://") or base_url.startswith("http://")):
            raise ValueError("SILICONFLOW_BASE_URL 必须是 http 或 https 地址")
        self.siliconflow_base_url = base_url
        self.siliconflow_api_key = self.siliconflow_api_key.strip()
        return self


settings = Settings()
