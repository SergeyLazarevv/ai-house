"""Graylog MCP client connector."""

from __future__ import annotations

import json
import os
import sys
from contextlib import AsyncExitStack
from typing import Any

from app.config import GraylogConfig

from .base import BaseConnector


def format_tools_for_llm(tools: list[dict[str, Any]]) -> str:
    """Текст каталога MCP tools для system prompt."""
    parts = [
        "## Каталог инструментов Graylog MCP",
        "Вызывай только перечисленные имена. Аргументы — JSON по схеме ниже.",
    ]
    for spec in tools:
        parts.append(f"\n### {spec['name']}\n{spec.get('description', '')}\n```json")
        parts.append(json.dumps(spec.get("inputSchema", {}), ensure_ascii=False, indent=2))
        parts.append("```")
    return "\n".join(parts)


def _tool_result_to_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    content = getattr(result, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            text = getattr(item, "text", None)
            if isinstance(text, str):
                parts.append(text)
                continue
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
        if parts:
            return "\n".join(parts)
    return str(result)


def _tool_field(tool: Any, key: str, default: Any = None) -> Any:
    if isinstance(tool, dict):
        return tool.get(key, default)
    value = getattr(tool, key, None)
    if value is not None:
        return value
    if key == "inputSchema":
        value = getattr(tool, "input_schema", None)
        if value is not None:
            return value
    return default


class GraylogConnector(BaseConnector):
    def __init__(self, config: GraylogConfig) -> None:
        self._cfg = config
        self._tools: list[dict[str, Any]] = []
        self._session: Any = None

    @property
    def is_configured(self) -> bool:
        return self._cfg.enabled

    async def connect(self, stack: AsyncExitStack) -> None:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:  # pragma: no cover - depends on runtime env
            raise RuntimeError("Для Graylog MCP нужен пакет `mcp`. Установите зависимости заново.") from exc

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "app.mcp_servers.graylog"],
            env=os.environ.copy(),
        )
        read, write = await stack.enter_async_context(stdio_client(params))
        session = ClientSession(read, write)
        self._session = await stack.enter_async_context(session)
        await self._session.initialize()

        raw_tools = await self._session.list_tools()
        tools = getattr(raw_tools, "tools", raw_tools)
        self._tools = []
        for tool in tools or []:
            self._tools.append(
                {
                    "name": _tool_field(tool, "name"),
                    "description": _tool_field(tool, "description", ""),
                    "inputSchema": _tool_field(tool, "inputSchema", {}),
                }
            )

    @property
    def tools(self) -> list[dict[str, Any]]:
        return self._tools

    async def call_tool(self, name: str, args: dict) -> str:
        if not self._session:
            return "Ошибка: MCP-сессия Graylog не подключена."
        try:
            result = await self._session.call_tool(name, args or {})
        except Exception as exc:  # pragma: no cover - runtime transport errors
            return f"Ошибка MCP Graylog: {exc!s}"
        return _tool_result_to_text(result)
