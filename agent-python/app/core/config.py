from pydantic_settings import BaseSettings, SettingsConfigDict


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
    redis_host: str = ""
    redis_port: int = 6379
    redis_password: str = ""
    enterprise_infra_enabled: bool = False


settings = Settings()
