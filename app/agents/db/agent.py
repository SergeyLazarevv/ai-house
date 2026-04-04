"""Агент БД: только Postgres (read-only запросы). Не импортирует других агентов."""

from __future__ import annotations

from contextlib import AsyncExitStack

from app.agents.base import BaseAgent
from app.agents.prompt_loader import load_agent_prompt
from app.config import AppConfig
from app.shared.connectors.postgres import PostgresConnector
from app.shared.llm import build_llm
from app.shared.tool_parser import parse_tool_call


class DbAgent(BaseAgent):
    """Специализированный агент: инструмент только query (Postgres MCP)."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._llm = build_llm(config)
        self._connector = PostgresConnector(config.postgres.dsn or "")

    async def run(self, message: str, context: str = "") -> str:
        if not self._connector.is_configured:
            return "Агент БД: Postgres не настроен (POSTGRES_MCP_DSN)."
        user_text = f"{context}\n\nЗапрос: {message}" if context else message
        system = load_agent_prompt("db")
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
        return "Агент БД: лимит итераций."
