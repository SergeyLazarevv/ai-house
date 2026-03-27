"""Graylog MCP: заглушка до реализации MCP-клиента."""

from __future__ import annotations

from contextlib import AsyncExitStack

from .base import BaseConnector


class GraylogConnector(BaseConnector):
    def __init__(self, url: str, auth: str) -> None:
        self._url = url
        self._auth = auth
        self._tools: list[dict] = []

    @property
    def is_configured(self) -> bool:
        return bool(self._url and self._auth)

    async def connect(self, stack: AsyncExitStack) -> None:
        self._tools = [
            {"name": "search_messages", "description": "Поиск сообщений в Graylog", "inputSchema": {}},
            {"name": "aggregate_messages", "description": "Агрегация по полям", "inputSchema": {}},
        ]

    @property
    def tools(self) -> list[dict]:
        return self._tools

    async def call_tool(self, name: str, args: dict) -> str:
        return f"[Graylog stub] Инструмент {name} — реализуйте MCP-вызов в этом коннекторе."
