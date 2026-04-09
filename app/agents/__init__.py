"""Специализированные агенты. Оркестратор импортирует отсюда; агенты друг друга не импортируют."""

from .base import BaseAgent
from .logs.agent import LogsAgent
from .db.agent import DbAgent
from .code.agent import CodeAgent


def get_agent(role: str):
    """Возвращает класс агента по роли (вызывается из узлов графа)."""
    mapping = {"logs": LogsAgent, "db": DbAgent, "code": CodeAgent}
    return mapping.get(role)
