"""Конфигурация из переменных окружения (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    return value in {"1", "true", "yes", "y", "on"}


@dataclass
class GraylogConfig:
    """Логовый агент включён/выключен. MCP-сервер читает Graylog_* env напрямую."""

    enabled: bool = True

    @classmethod
    def from_env(cls) -> "GraylogConfig":
        return cls(enabled=_env_bool("AGENT_LOGS_ENABLED", True))

    @property
    def is_configured(self) -> bool:
        return self.enabled


@dataclass
class PostgresConfig:
    dsn: str | None
    enabled: bool = True

    @classmethod
    def from_env(cls) -> "PostgresConfig":
        return cls(
            dsn=(os.getenv("POSTGRES_MCP_DSN") or "").strip() or None,
            enabled=_env_bool("AGENT_DB_ENABLED", True),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.dsn)


@dataclass
class GitLabConfig:
    url: str
    token: str
    enabled: bool = True

    @classmethod
    def from_env(cls) -> "GitLabConfig":
        return cls(
            url=(os.getenv("GITLAB_URL") or "https://gitlab.com").strip().rstrip("/"),
            token=(os.getenv("GITLAB_TOKEN") or "").strip(),
            enabled=_env_bool("AGENT_CODE_ENABLED", True),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.url)


@dataclass
class AppConfig:
    graylog: GraylogConfig
    postgres: PostgresConfig
    gitlab: GitLabConfig
    yandex_api_key: str | None
    yandex_catalog_id: str | None
    # LLM: yandex | anthropic | openai (см. app.shared.llm.build_llm)
    llm_provider: str = "yandex"
    yandex_model: str = "yandexgpt-lite"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-3-5-sonnet-20241022"
    anthropic_api_version: str = "2023-06-01"
    anthropic_max_tokens: int = 8192
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    # Максимум шагов решения оркестратора (каждый вызов специалиста + решения «что дальше»)
    graph_supervisor_max_steps: int = 10

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            graylog=GraylogConfig.from_env(),
            postgres=PostgresConfig.from_env(),
            gitlab=GitLabConfig.from_env(),
            yandex_api_key=os.getenv("YANDEX_API_KEY") or os.getenv("YANDEX_OAUTH"),
            yandex_catalog_id=os.getenv("YANDEX_CATALOG_ID"),
            llm_provider=os.getenv("LLM_PROVIDER", "yandex").strip(),
            yandex_model=os.getenv("YANDEX_MODEL", "yandexgpt-lite"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022"),
            anthropic_api_version=os.getenv("ANTHROPIC_API_VERSION", "2023-06-01"),
            anthropic_max_tokens=int(os.getenv("ANTHROPIC_MAX_TOKENS", "8192")),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            graph_supervisor_max_steps=int(os.getenv("GRAPH_SUPERVISOR_MAX_STEPS", "10")),
        )

    def llm_status(self) -> str:
        p = (self.llm_provider or "yandex").strip().lower()
        if p in ("yandex", "yc", "yandexgpt"):
            if self.yandex_api_key and self.yandex_catalog_id:
                return "ok"
            return "не настроен"
        if p in ("anthropic", "claude"):
            return "ok" if self.anthropic_api_key else "не настроен"
        if p in ("openai", "openai_compatible", "compatible"):
            return "ok" if self.openai_api_key else "не настроен"
        return f"неизвестный провайдер: {self.llm_provider}"
