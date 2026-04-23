"""Запуск скомпилированного графа LangGraph (AppConfig передаётся в configurable)."""

from __future__ import annotations

import logging
import uuid

from app.config import AppConfig
from app.orchestration.graph import get_compiled_graph

log = logging.getLogger("ai_house.orchestration.runner")

_AGENT_LABELS: dict[str, str] = {
    "orchestrator": "оркестратор",
    "logs": "логи",
    "db": "БД",
    "code": "код",
    "synthesize": "сводка",
}


def _append_agents_footer(text: str, agents_used: list[str] | None) -> str:
    if not agents_used:
        return text
    seen: set[str] = set()
    labels: list[str] = []
    for key in agents_used:
        if key in seen:
            continue
        seen.add(key)
        label = _AGENT_LABELS.get(key, key)
        labels.append(label)
    if not labels:
        return text
    line = "Задействовано: " + ", ".join(labels)
    if not (text or "").strip():
        return line
    return f"{text.rstrip()}\n\n---\n_{line}_"


async def run_graph(message: str, config: AppConfig, trace_id: str | None = None) -> str:
    trace_id = trace_id or str(uuid.uuid4())[:8]
    msg = (message or "").strip()
    if not msg:
        return "Сообщение не может быть пустым."

    graph = get_compiled_graph()
    result = await graph.ainvoke(
        {
            "user_message": msg,
            "trace_id": trace_id,
            "logs_invocations": 0,
            "db_invocations": 0,
            "code_invocations": 0,
            "last_specialist_role": "",
            "last_specialist_status": "none",
            "last_specialist_error": "",
        },
        config={"configurable": {"app_config": config}},
    )
    out = result.get("final_response")
    agents = result.get("agents_used")
    if out:
        return _append_agents_footer(out, agents if isinstance(agents, list) else None)
    err = result.get("error")
    if err:
        return err
    log.warning("[%s] Пустой результат графа: %s", trace_id, result)
    return "Пустой ответ. Повторите запрос или проверьте /api/status."
