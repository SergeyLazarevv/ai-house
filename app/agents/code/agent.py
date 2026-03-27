"""Агент кода: только GitLab (файлы, проекты, issues, MR). Не импортирует других агентов."""

from __future__ import annotations

from contextlib import AsyncExitStack

from app.agents.base import BaseAgent
from app.config import AppConfig
from app.shared.connectors.gitlab import GitLabConnector
from app.shared.llm import build_llm
from app.shared.tool_parser import parse_tool_call


class CodeAgent(BaseAgent):
    """Специализированный агент: инструменты только GitLab REST."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._llm = build_llm(config)
        self._connector = GitLabConnector(config.gitlab.url, config.gitlab.token)

    async def run(self, message: str, context: str = "") -> str:
        if not self._connector.is_configured:
            return "Агент кода: GitLab не настроен (GITLAB_URL)."
        user_text = f"{context}\n\nЗапрос: {message}" if context else message
        system = "Ты — агент работы с кодом в GitLab. Используй gitlab_get_file, gitlab_list_projects и др. Читай файлы по путям из стектрейсов. Отвечай с фрагментами кода."
        async with AsyncExitStack() as stack:
            await self._connector.connect(stack)
            tool_names = self._connector.tool_names
            messages = [
                {"role": "system", "content": system + "\nИнструменты: " + ", ".join(tool_names)},
                {"role": "user", "content": user_text},
            ]
            for _ in range(5):
                reply = await self._llm.complete(messages)
                name, args = parse_tool_call(reply, tool_names)
                if not name:
                    return reply.strip()
                result = await self._connector.call_tool(name, args or {})
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Результат {name}]:\n{result}\n\nДай итоговый ответ."})
        return "Агент кода: лимит итераций."
