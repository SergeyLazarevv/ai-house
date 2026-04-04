"""Состояние графа LangGraph между узлами."""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class GraphState(TypedDict, total=False):
    """Общее состояние: узлы дописывают поля через merge."""

    user_message: str
    trace_id: str
    route: str  # опционально; основной поток — supervisor
    resolved_route: str
    # Идентификаторы этапов: logs|db|code|general|synthesize — накапливаются по ходу графа
    agents_used: Annotated[list[str], operator.add]
    db_result: str
    logs_result: str
    code_result: str
    final_response: str
    error: str
    # Цикл supervisor → специалист → supervisor
    supervisor_step: int
    supervisor_next: str  # db|logs|code|general|finish|end
    supervisor_reason: str
    # Задание текущего шага: формулирует только оркестратор (не сырой вопрос пользователя)
    supervisor_task: str
    # Краткие факты для этого вызова (id, даты, имя input); не полные логи/дампы
    supervisor_context_hint: str