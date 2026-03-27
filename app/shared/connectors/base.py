"""Базовый интерфейс коннектора к внешнему сервису (MCP или REST)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AsyncExitStack


class BaseConnector(ABC):
    @property
    @abstractmethod
    def is_configured(self) -> bool:
        ...

    @abstractmethod
    async def connect(self, stack: AsyncExitStack) -> None:
        ...

    @property
    @abstractmethod
    def tools(self) -> list[dict]:
        ...

    @property
    def tool_names(self) -> list[str]:
        return [t["name"] for t in self.tools]

    @abstractmethod
    async def call_tool(self, name: str, args: dict) -> str:
        ...
