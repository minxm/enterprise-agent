from urllib.parse import urlparse

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EMBEDDING_MODEL_NAME = "Qwen/Qwen3-Embedding-0.6B"
EMBEDDING_WIDTH = 1024
CHAT_MODEL_NAME = "Qwen/Qwen2.5-7B-Instruct"
RERANKER_MODEL_NAME = "Qwen/Qwen3-Reranker-0.6B"
RERANKER_MODELS = frozenset(
    {
        "Qwen/Qwen3-Reranker-0.6B",
        "Qwen/Qwen3-Reranker-4B",
        "Qwen/Qwen3-Reranker-8B",
    }
)
RERANKER_MAX_CANDIDATES = 20
RAG_RETRIEVAL_STRATEGIES = frozenset({"vector", "hybrid", "hybrid_rerank"})
RAG_FAILURE_POLICIES = frozenset({"strict", "fallback"})


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
    chat_model: str = CHAT_MODEL_NAME
    chat_timeout_seconds: float = 60
    chat_max_tokens: int = 1024
    reranker_enabled: bool = False
    reranker_model: str = RERANKER_MODEL_NAME
    reranker_base_url: str = ""
    reranker_api_key: str = ""
    reranker_timeout_seconds: float = 30
    reranker_top_n: int = RERANKER_MAX_CANDIDATES
    rag_retrieval_strategy: str = "vector"
    rag_retrieval_top_k: int = 5
    rag_retrieval_candidate_k: int = RERANKER_MAX_CANDIDATES
    rag_reranker_failure_policy: str = "strict"
    rag_eval_judge_enabled: bool = False
    rag_eval_judge_model: str = CHAT_MODEL_NAME
    rag_eval_judge_timeout_seconds: float = 60
    agent_internal_token: str = ""
    java_base_url: str = ""
    java_timeout_seconds: float = 5.0

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
        chat_model = self.chat_model.strip()
        if not chat_model or len(chat_model) > 200:
            raise ValueError("CHAT_MODEL 不能为空")
        if chat_model == EMBEDDING_MODEL_NAME:
            raise ValueError("CHAT_MODEL 不能使用 Embedding 模型")
        chat_timeout = self.chat_timeout_seconds
        if isinstance(chat_timeout, bool) or not isinstance(chat_timeout, (int, float)) or not 0 < float(chat_timeout) <= 120:
            raise ValueError("CHAT_TIMEOUT_SECONDS 必须在 0 到 120 秒之间")
        if type(self.chat_max_tokens) is not int or not 1 <= self.chat_max_tokens <= 4096:
            raise ValueError("CHAT_MAX_TOKENS 必须在 1 到 4096 之间")
        self.chat_model = chat_model
        if self.reranker_model not in RERANKER_MODELS:
            raise ValueError("RERANKER_MODEL 必须是 Qwen/Qwen3-Reranker-0.6B、4B 或 8B")
        if self.reranker_model == EMBEDDING_MODEL_NAME:
            raise ValueError("RERANKER_MODEL 不能使用 Embedding 模型")
        timeout = self.reranker_timeout_seconds
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < float(timeout) <= 120:
            raise ValueError("RERANKER_TIMEOUT_SECONDS 必须在 0 到 120 秒之间")
        if type(self.reranker_top_n) is not int or not 1 <= self.reranker_top_n <= RERANKER_MAX_CANDIDATES:
            raise ValueError("RERANKER_TOP_N 必须在 1 到 20 之间")
        rerank_url = self.reranker_base_url.strip().rstrip("/")
        if rerank_url and not (rerank_url.startswith("https://") or rerank_url.startswith("http://")):
            raise ValueError("RERANKER_BASE_URL 必须是 http 或 https 地址")
        self.reranker_base_url = rerank_url
        self.reranker_api_key = self.reranker_api_key.strip()
        if self.rag_retrieval_strategy not in RAG_RETRIEVAL_STRATEGIES:
            raise ValueError("RAG_RETRIEVAL_STRATEGY 必须是 vector、hybrid 或 hybrid_rerank")
        if self.rag_reranker_failure_policy not in RAG_FAILURE_POLICIES:
            raise ValueError("RAG_RERANKER_FAILURE_POLICY 必须是 strict 或 fallback")
        if type(self.rag_retrieval_top_k) is not int or not 1 <= self.rag_retrieval_top_k <= RERANKER_MAX_CANDIDATES:
            raise ValueError("RAG_RETRIEVAL_TOP_K 必须在 1 到 20 之间")
        if (
            type(self.rag_retrieval_candidate_k) is not int
            or not 1 <= self.rag_retrieval_candidate_k <= RERANKER_MAX_CANDIDATES
        ):
            raise ValueError("RAG_RETRIEVAL_CANDIDATE_K 必须在 1 到 20 之间")
        if self.rag_retrieval_candidate_k < self.rag_retrieval_top_k:
            raise ValueError("RAG_RETRIEVAL_CANDIDATE_K 不能小于 RAG_RETRIEVAL_TOP_K")
        if type(self.rag_eval_judge_enabled) is not bool:
            raise ValueError("RAG_EVAL_JUDGE_ENABLED 必须是布尔值")
        judge_model = self.rag_eval_judge_model.strip()
        if not judge_model or len(judge_model) > 200:
            raise ValueError("RAG_EVAL_JUDGE_MODEL 不能为空")
        if judge_model == EMBEDDING_MODEL_NAME:
            raise ValueError("RAG_EVAL_JUDGE_MODEL 不能使用 Embedding 模型")
        self.rag_eval_judge_model = judge_model
        judge_timeout = self.rag_eval_judge_timeout_seconds
        if isinstance(judge_timeout, bool) or not isinstance(judge_timeout, (int, float)) or not 0 < float(judge_timeout) <= 120:
            raise ValueError("RAG_EVAL_JUDGE_TIMEOUT_SECONDS 必须在 0 到 120 秒之间")
        token = self.agent_internal_token.strip()
        if any(char in token for char in "\r\n"):
            raise ValueError("AGENT_INTERNAL_TOKEN 不能包含换行")
        if len(token) > 256:
            raise ValueError("AGENT_INTERNAL_TOKEN 过长")
        self.agent_internal_token = token
        java_url = self.java_base_url.strip().rstrip("/")
        if java_url:
            parsed = urlparse(java_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError("JAVA_BASE_URL 必须是不含账号的 http 或 https 地址")
        self.java_base_url = java_url
        java_timeout = self.java_timeout_seconds
        if isinstance(java_timeout, bool) or not isinstance(java_timeout, (int, float)) or not 0 < float(java_timeout) <= 30:
            raise ValueError("JAVA_TIMEOUT_SECONDS 必须在 0 到 30 秒之间")
        return self


settings = Settings()
