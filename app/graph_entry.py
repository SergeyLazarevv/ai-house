"""Точка входа: запуск графа LangGraph по пользовательскому сообщению."""

from __future__ import annotations

import logging
import uuid

from app.config import AppConfig
from app.orchestration.runner import run_graph

log = logging.getLogger("ai_house.graph")

# Типы маршрутов (для тестов и расширений)
TASK_LOGS = "logs"
TASK_DB = "db"
TASK_CODE = "code"
TASK_LOGS_THEN_CODE = "logs_then_code"


async def run_user_request(message: str, config: AppConfig, trace_id: str | None = None) -> str:
    trace_id = trace_id or str(uuid.uuid4())[:8]
    log.info("[%s] Запрос: %d симв.", trace_id, len(message or ""))
    return await run_graph(message, config, trace_id)
