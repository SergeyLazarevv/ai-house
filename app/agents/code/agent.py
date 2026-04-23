"""Агент кода: только GitLab (файлы, проекты, issues, MR). Не импортирует других агентов."""

from __future__ import annotations

from contextlib import AsyncExitStack

from app.agents.base import BaseAgent
from app.agents.prompt_loader import load_agent_prompt
from app.config import AppConfig
from app.shared.connectors.gitlab import GitLabConnector, format_tools_for_llm
from app.shared.llm import build_llm
from app.shared.tool_parser import parse_tool_call


class CodeAgent(BaseAgent):
    """Специализированный агент: инструменты только GitLab MCP."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._llm = build_llm(config)
        self._connector = GitLabConnector(config.gitlab)

    async def run(self, message: str, context: str = "") -> str:
        if not self._connector.is_configured:
            return "Агент кода: GitLab не настроен (GITLAB_URL / GITLAB_TOKEN)."
        user_text = f"{context}\n\nЗапрос: {message}" if context else message
        async with AsyncExitStack() as stack:
            try:
                await self._connector.connect(stack)
            except Exception as e:
                return f"Агент кода: не удалось поднять GitLab-коннектор: {e!s}"
            tool_names = self._connector.tool_names
            system = "\n\n".join(
                [
                    load_agent_prompt("code").strip(),
                    format_tools_for_llm(self._connector.tools),
                    "Инструменты: " + ", ".join(tool_names),
                ]
            )
            messages = [
                {"role": "system", "content": system},
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
