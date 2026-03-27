"""Состояние графа LangGraph между узлами."""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class GraphState(TypedDict, total=False):
    """Общее состояние: узлы дописывают поля через merge."""

    user_message: str
    trace_id: str
    route: str  # сырая классификация: logs|db|code|logs_chain|investigate|general|unknown
    resolved_route: str  # после учёта включённых агентов
    # Идентификаторы этапов: logs|db|code|general|synthesize — накапливаются по ходу графа
    agents_used: Annotated[list[str], operator.add]
    db_result: str
    logs_result: str
    code_result: str
    final_response: str
    error: str
