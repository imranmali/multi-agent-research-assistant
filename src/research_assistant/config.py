"""Runtime configuration loaded from environment variables (and an optional ``.env`` file)."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LLMProvider = Literal["anthropic", "mock"]
JobBackend = Literal["memory", "redis"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    """All tunables for the service. Every field maps to an upper-case env var."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- service -----------------------------------------------------------------
    app_name: str = "multi-agent-research-assistant"
    environment: Literal["development", "staging", "production", "test"] = "development"
    log_level: str = "INFO"
    log_json: bool = True

    # --- LLM ---------------------------------------------------------------------
    llm_provider: LLMProvider = "mock"
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: Effort = "medium"
    anthropic_max_tokens: int = Field(default=16000, ge=1024, le=128000)
    anthropic_timeout_seconds: float = Field(default=600.0, gt=0)
    anthropic_max_retries: int = Field(default=3, ge=0, le=10)
    anthropic_enable_fallbacks: bool = True
    max_search_uses: int = Field(default=5, ge=1, le=20)

    # --- research pipeline defaults (overridable per request) ----------------------
    max_subquestions: int = Field(default=4, ge=1, le=10)
    max_parallel_research: int = Field(default=4, ge=1, le=16)
    max_revisions: int = Field(default=1, ge=0, le=5)

    # --- jobs ----------------------------------------------------------------------
    job_backend: JobBackend = "memory"
    redis_url: str = "redis://localhost:6379/0"
    job_ttl_seconds: int = Field(default=7 * 24 * 3600, ge=60)
    worker_concurrency: int = Field(default=2, ge=1, le=64)
    worker_shutdown_grace_seconds: float = Field(default=30.0, ge=0)
    queue_name: str = "research:queue"

    # --- API -----------------------------------------------------------------------
    api_keys: Annotated[list[str], NoDecode] = Field(default_factory=list)
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    max_list_limit: int = Field(default=100, ge=1, le=1000)
    sse_poll_interval_seconds: float = Field(default=0.5, gt=0)

    @field_validator("api_keys", "cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Allow ``API_KEYS=a,b`` in addition to JSON lists."""
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
