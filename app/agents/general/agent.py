"""Общий агент: только LLM, без инструментов (малый talk, вопросы не про инфраструктуру)."""

from __future__ import annotations

from app.agents.base import BaseAgent
from app.config import AppConfig
from app.shared.llm import build_llm


class GeneralAgent(BaseAgent):
    def __init__(self, config: AppConfig) -> None:
        self._config = config

    async def run(self, message: str, context: str = "") -> str:
        if self._config.llm_status() != "ok":
            return (
                "Общий агент недоступен: настройте LLM (см. LLM_PROVIDER и ключи в .env)."
            )
        llm = build_llm(self._config)
        system = (
            "Ты дружелюбный ассистент. Отвечай кратко и по делу. "
            "У тебя нет доступа к логам, базам данных и репозиториям — не выдумывай их. "
            "Если вопрос про инфраструктуру, подскажи обратиться к соответствующим инструментам в системе."
        )
        user_text = f"{context}\n\nВопрос: {message}" if context else message
        reply = await llm.complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ]
        )
        return (reply or "").strip()
