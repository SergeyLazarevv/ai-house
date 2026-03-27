"""Агент логов: только Graylog (поиск, агрегация, стримы). Не импортирует других агентов."""

from __future__ import annotations

from contextlib import AsyncExitStack

from app.agents.base import BaseAgent
from app.config import AppConfig
from app.shared.connectors.graylog import GraylogConnector
from app.shared.llm import build_llm
from app.shared.tool_parser import parse_tool_call


class LogsAgent(BaseAgent):
    """Специализированный агент: инструменты только Graylog MCP."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._llm = build_llm(config)
        self._connector = GraylogConnector(config.graylog.url, config.graylog.auth)

    async def run(self, message: str, context: str = "") -> str:
        if not self._connector.is_configured:
            return "Агент логов: Graylog не настроен (GRAYLOG_MCP_URL, GRAYLOG_MCP_AUTH)."
        user_text = f"{context}\n\nЗапрос: {message}" if context else message
        system = "Ты — агент анализа логов. Используй только инструменты Graylog: search_messages, aggregate_messages. Отвечай кратко по сути."
        async with AsyncExitStack() as stack:
            await self._connector.connect(stack)
            tools = self._connector.tools
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
                messages.append({"role": "user", "content": f"[Результат {name}]:\n{result}\n\nДай итоговый ответ пользователю."})
        return "Агент логов: лимит итераций."
