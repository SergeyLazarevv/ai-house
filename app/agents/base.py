"""Базовый агент: ReAct-цикл с ограниченным набором инструментов. Наследуют logs/db/code."""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseAgent(ABC):
    """Агент получает сообщение и контекст, возвращает строку-ответ. Инструменты задаются при создании."""

    @abstractmethod
    async def run(self, message: str, context: str = "") -> str:
        """
        Выполнить задачу. context — результат предыдущего агента в цепочке (если есть).
        Возвращает текстовый ответ.
        """
        ...
