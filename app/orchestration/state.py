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
    # Идентификаторы этапов: orchestrator|logs|db|code|synthesize — накапливаются по ходу графа
    agents_used: Annotated[list[str], operator.add]
    db_result: str
    logs_result: str
    code_result: str
    final_response: str
    error: str
    # Цикл supervisor → специалист → supervisor
    supervisor_step: int
    supervisor_next: str  # db|logs|code|finish|end
    supervisor_reason: str
    supervisor_answer_mode: str  # direct|delegate
    supervisor_target_domain: str  # db|logs|code|none
    supervisor_needs_external_data: bool
    supervisor_user_explicitly_requested_source: bool
    supervisor_confidence: float
    # Задание текущего шага: формулирует только оркестратор (не сырой вопрос пользователя)
    supervisor_task: str
    # Краткие факты для этого вызова (id, даты, имя input); не полные логи/дампы
    supervisor_context_hint: str
    # Достигнут GRAPH_SUPERVISOR_MAX_STEPS — сводка должна явно это отразить
    supervisor_truncated: bool
    # После успешного ответа специалиста — отпечаток (user_message+task+hint); повтор того же delegate → finish
    logs_success_fingerprint: str
    db_success_fingerprint: str
    code_success_fingerprint: str
    # Счётчики вызовов специалистов за один пользовательский запрос (предохранитель от циклов)
    logs_invocations: Annotated[int, operator.add]
    db_invocations: Annotated[int, operator.add]
    code_invocations: Annotated[int, operator.add]
    # Единый контракт последнего вызова специалиста: success|error|none и причина ошибки.
    last_specialist_role: str
    last_specialist_status: str
    last_specialist_error: str