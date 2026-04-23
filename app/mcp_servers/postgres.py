"""Postgres MCP server: read-only SQL tools for the DB agent."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import FastMCP


mcp = FastMCP("postgres")

_POOL: Any = None
_POOL_LOCK = asyncio.Lock()
_DEFAULT_SCHEMA = "public"
_DEFAULT_LIMIT = 20
_DEFAULT_TIMEOUT_MS = 30_000


@dataclass(frozen=True)
class PostgresConfig:
    enabled: bool = True
    dsn: str | None = None
    default_schema: str = _DEFAULT_SCHEMA
    default_limit: int = _DEFAULT_LIMIT
    statement_timeout_ms: int = _DEFAULT_TIMEOUT_MS

    @classmethod
    def from_env(cls) -> "PostgresConfig":
        dsn = (
            os.getenv("POSTGRES_MCP_DSN")
            or os.getenv("POSTGRES_URL")
            or os.getenv("POSTGRES_DSN")
            or ""
        ).strip() or None
        default_limit_raw = (os.getenv("POSTGRES_DEFAULT_LIMIT") or str(_DEFAULT_LIMIT)).strip()
        timeout_raw = (os.getenv("POSTGRES_STATEMENT_TIMEOUT_MS") or str(_DEFAULT_TIMEOUT_MS)).strip()
        try:
            default_limit = max(1, min(500, int(default_limit_raw)))
        except (TypeError, ValueError):
            default_limit = _DEFAULT_LIMIT
        try:
            statement_timeout_ms = max(1, int(timeout_raw))
        except (TypeError, ValueError):
            statement_timeout_ms = _DEFAULT_TIMEOUT_MS
        return cls(
            enabled=_env_bool("AGENT_DB_ENABLED", True),
            dsn=dsn,
            default_schema=(os.getenv("POSTGRES_SCHEMA") or _DEFAULT_SCHEMA).strip() or _DEFAULT_SCHEMA,
            default_limit=default_limit,
            statement_timeout_ms=statement_timeout_ms,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.enabled and self.dsn)


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _compact_json(data: Any, limit: int | None = None) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if limit and len(text) > limit:
        return text[: limit - 40] + "\n… [обрезано]"
    return text


def _config_error() -> str | None:
    cfg = PostgresConfig.from_env()
    if not cfg.enabled:
        return "Postgres-агент отключён (AGENT_DB_ENABLED=false)."
    if cfg.dsn:
        return None
    return "Postgres не настроен: укажите POSTGRES_MCP_DSN, POSTGRES_URL или POSTGRES_DSN."


def _sql_looks_readonly(sql: str) -> bool:
    text = (sql or "").strip()
    if not text:
        return False
    if ";" in text.rstrip(";"):
        return False
    first = re.match(r"(?is)^\s*(select|with|values|explain)\b", text)
    if not first:
        return False
    forbidden = re.search(
        r"(?is)\b(insert|update|delete|merge|create|alter|drop|truncate|grant|revoke|"
        r"vacuum|copy|call|do|prepare|execute|set)\b",
        text,
    )
    return not bool(forbidden)


async def _ensure_pool() -> Any:
    global _POOL
    cfg = PostgresConfig.from_env()
    if _POOL is not None or not cfg.is_configured:
        return _POOL
    async with _POOL_LOCK:
        if _POOL is not None or not cfg.is_configured:
            return _POOL
        try:
            import asyncpg
        except ImportError as exc:  # pragma: no cover - runtime dependency
            raise RuntimeError("Для Postgres MCP нужен пакет `asyncpg`. Добавьте зависимости заново.") from exc
        _POOL = await asyncpg.create_pool(
            dsn=cfg.dsn,
            min_size=1,
            max_size=4,
            command_timeout=max(1, cfg.statement_timeout_ms // 1000),
        )
    return _POOL


def _default_limit(raw: int | None = None) -> int:
    cfg = PostgresConfig.from_env()
    if raw is None:
        return cfg.default_limit
    try:
        return max(1, min(500, int(raw)))
    except (TypeError, ValueError):
        return cfg.default_limit


def _normalize_sql(sql: str) -> str:
    text = (sql or "").strip()
    if text.endswith(";"):
        text = text.rstrip(";").strip()
    return text


@mcp.tool()
async def list_tables(schema: str | None = None) -> str:
    cfg = PostgresConfig.from_env()
    err = _config_error()
    if err:
        return err
    pool = await _ensure_pool()
    if pool is None:
        return "Postgres: не удалось создать пул подключения."
    schema_name = (schema or cfg.default_schema).strip() or cfg.default_schema
    try:
        async with pool.acquire() as conn:
            await conn.execute(f"SET statement_timeout = {int(cfg.statement_timeout_ms)}")
            rows = await conn.fetch(
                """
                SELECT table_schema, table_name
                FROM information_schema.tables
                WHERE table_type = 'BASE TABLE' AND table_schema = $1
                ORDER BY table_name
                """,
                schema_name,
            )
    except Exception as exc:  # pragma: no cover - runtime/db errors
        return f"Postgres: ошибка list_tables: {exc!s}"
    payload = {
        "schema": schema_name,
        "count": len(rows),
        "tables": [dict(row) for row in rows],
    }
    return _compact_json(payload, limit=40_000)


@mcp.tool()
async def describe_table(table: str, schema: str | None = None) -> str:
    cfg = PostgresConfig.from_env()
    err = _config_error()
    if err:
        return err
    pool = await _ensure_pool()
    if pool is None:
        return "Postgres: не удалось создать пул подключения."
    table_name = (table or "").strip()
    if not table_name:
        return "Postgres: укажите имя таблицы."
    schema_name = (schema or cfg.default_schema).strip() or cfg.default_schema
    try:
        async with pool.acquire() as conn:
            await conn.execute(f"SET statement_timeout = {int(cfg.statement_timeout_ms)}")
            rows = await conn.fetch(
                """
                SELECT column_name, data_type, is_nullable, column_default, ordinal_position
                FROM information_schema.columns
                WHERE table_schema = $1 AND table_name = $2
                ORDER BY ordinal_position
                """,
                schema_name,
                table_name,
            )
    except Exception as exc:  # pragma: no cover - runtime/db errors
        return f"Postgres: ошибка describe_table: {exc!s}"
    payload = {
        "schema": schema_name,
        "table": table_name,
        "count": len(rows),
        "columns": [dict(row) for row in rows],
    }
    return _compact_json(payload, limit=40_000)


@mcp.tool()
async def query(sql: str, params: list[Any] | None = None, limit: int | None = None) -> str:
    cfg = PostgresConfig.from_env()
    err = _config_error()
    if err:
        return err
    cleaned_sql = _normalize_sql(sql)
    if not _sql_looks_readonly(cleaned_sql):
        return "Postgres: поддерживаются только read-only запросы SELECT/WITH/VALUES/EXPLAIN без DML/DDL."
    pool = await _ensure_pool()
    if pool is None:
        return "Postgres: не удалось создать пул подключения."
    use_limit = _default_limit(limit)
    params = params or []
    if not isinstance(params, list):
        params = [params]
    if re.match(r"(?is)^\s*(select|with|values)\b", cleaned_sql) and not re.search(r"(?is)\blimit\b", cleaned_sql):
        executed_sql = f"SELECT * FROM ({cleaned_sql}) AS _query_result LIMIT {use_limit}"
    else:
        executed_sql = cleaned_sql
    try:
        async with pool.acquire() as conn:
            await conn.execute(f"SET statement_timeout = {int(cfg.statement_timeout_ms)}")
            rows = await conn.fetch(executed_sql, *params)
    except Exception as exc:  # pragma: no cover - runtime/db errors
        return f"Postgres: ошибка выполнения запроса: {exc!s}"
    payload = {
        "sql": cleaned_sql,
        "executed_sql": executed_sql,
        "limit": use_limit,
        "row_count": len(rows),
        "columns": list(rows[0].keys()) if rows else [],
        "rows": [dict(row) for row in rows[:use_limit]],
    }
    return _compact_json(payload, limit=80_000)


if __name__ == "__main__":
    mcp.run()
