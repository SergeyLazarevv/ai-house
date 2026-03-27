"""GitLab REST: заглушка до реализации вызовов API."""

from __future__ import annotations

from contextlib import AsyncExitStack

from .base import BaseConnector


class GitLabConnector(BaseConnector):
    def __init__(self, url: str, token: str) -> None:
        self._url = url
        self._token = token
        self._tools: list[dict] = []

    @property
    def is_configured(self) -> bool:
        return bool(self._url)

    async def connect(self, stack: AsyncExitStack) -> None:
        self._tools = [
            {"name": "gitlab_get_file", "description": "Читать файл из репозитория", "inputSchema": {}},
            {"name": "gitlab_list_projects", "description": "Список проектов", "inputSchema": {}},
        ]

    @property
    def tools(self) -> list[dict]:
        return self._tools

    async def call_tool(self, name: str, args: dict) -> str:
        return f"[GitLab stub] Инструмент {name} — реализуйте REST-вызовы в этом коннекторе."
