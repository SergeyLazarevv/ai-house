"""Postgres MCP: заглушка до реализации MCP-клиента."""

from __future__ import annotations

from contextlib import AsyncExitStack

from .base import BaseConnector


class PostgresConnector(BaseConnector):
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._tools: list[dict] = []

    @property
    def is_configured(self) -> bool:
        return bool(self._dsn)

    async def connect(self, stack: AsyncExitStack) -> None:
        self._tools = [{"name": "query", "description": "Выполнить SQL (read-only)", "inputSchema": {}}]

    @property
    def tools(self) -> list[dict]:
        return self._tools

    async def call_tool(self, name: str, args: dict) -> str:
        return f"[Postgres stub] Инструмент {name} — реализуйте MCP-вызов в этом коннекторе."
