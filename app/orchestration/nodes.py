"""Узлы графа: вызов агентов и синтез."""

from __future__ import annotations

import logging

from langgraph.config import get_config

from app.agents import get_agent
from app.config import AppConfig
from app.orchestration.state import GraphState

log = logging.getLogger("ai_house.orchestration.nodes")


def _app_config() -> AppConfig:
    return get_config()["configurable"]["app_config"]


async def node_router(state: GraphState) -> dict:
    """Классификация + resolve по включённым агентам."""
    from app.orchestration.classifier import classify_route, resolve_route

    cfg = _app_config()
    msg = state.get("user_message") or ""
    route = await classify_route(msg, cfg)
    resolved = resolve_route(route, cfg)
    log.info(
        "[%s] route=%s resolved=%s router=%s",
        state.get("trace_id", ""),
        route,
        resolved,
        cfg.graph_router,
    )
    return {"route": route, "resolved_route": resolved}


async def node_run_logs(state: GraphState) -> dict:
    cfg = _app_config()
    if not cfg.graylog.enabled:
        return {"final_response": "Агент логов отключен (AGENT_LOGS_ENABLED=false)."}
    cls = get_agent("logs")
    if not cls:
        return {"final_response": "Агент логов недоступен."}
    agent = cls(cfg)
    text = await agent.run(state.get("user_message") or "")
    return {"final_response": text, "logs_result": text, "agents_used": ["logs"]}


async def node_run_db(state: GraphState) -> dict:
    cfg = _app_config()
    if not cfg.postgres.enabled:
        return {"final_response": "Агент БД отключен (AGENT_DB_ENABLED=false)."}
    cls = get_agent("db")
    if not cls:
        return {"final_response": "Агент БД недоступен."}
    agent = cls(cfg)
    text = await agent.run(state.get("user_message") or "")
    return {"final_response": text, "db_result": text, "agents_used": ["db"]}


async def node_run_code(state: GraphState) -> dict:
    cfg = _app_config()
    if not cfg.gitlab.enabled:
        return {"final_response": "Агент кода отключен (AGENT_CODE_ENABLED=false)."}
    cls = get_agent("code")
    if not cls:
        return {"final_response": "Агент кода недоступен."}
    agent = cls(cfg)
    text = await agent.run(state.get("user_message") or "")
    return {"final_response": text, "code_result": text, "agents_used": ["code"]}


async def node_run_logs_chain(state: GraphState) -> dict:
    cfg = _app_config()
    if not cfg.graylog.enabled or not cfg.gitlab.enabled:
        return {
            "final_response": "Цепочка логи→код недоступна: включите AGENT_LOGS_ENABLED и AGENT_CODE_ENABLED."
        }
    logs_cls, code_cls = get_agent("logs"), get_agent("code")
    if not logs_cls or not code_cls:
        return {"final_response": "Цепочка логи→код недоступна."}
    msg = state.get("user_message") or ""
    logs_agent = logs_cls(cfg)
    code_agent = code_cls(cfg)
    logs_result = await logs_agent.run(msg)
    code_result = await code_agent.run(
        "По результатам логов выше: найди пути к файлам и строкам в трейсах, получи фрагменты кода и кратко опиши причину ошибок.",
        context=logs_result,
    )
    final = f"## Результат по логам\n\n{logs_result}\n\n## Контекст кода\n\n{code_result}"
    return {
        "final_response": final,
        "logs_result": logs_result,
        "code_result": code_result,
        "agents_used": ["logs", "code"],
    }


async def node_inv_db_logs_pipeline(state: GraphState) -> dict:
    """Расследование без GitLab: БД → логи → далее synthesize."""
    cfg = _app_config()
    db_cls, logs_cls = get_agent("db"), get_agent("logs")
    if not db_cls or not cfg.postgres.enabled:
        return {"db_result": "[БД недоступна]"}
    if not logs_cls or not cfg.graylog.enabled:
        return {"logs_result": "[Логи недоступны]"}
    msg = state.get("user_message") or ""
    db_result = await db_cls(cfg).run(msg)
    logs_result = await logs_cls(cfg).run(msg, context=db_result)
    return {"db_result": db_result, "logs_result": logs_result, "agents_used": ["db", "logs"]}


async def node_inv_db(state: GraphState) -> dict:
    cfg = _app_config()
    cls = get_agent("db")
    if not cls or not cfg.postgres.enabled:
        return {"db_result": "", "error": "БД недоступна для расследования."}
    agent = cls(cfg)
    text = await agent.run(state.get("user_message") or "")
    return {"db_result": text, "agents_used": ["db"]}


async def node_inv_logs(state: GraphState) -> dict:
    cfg = _app_config()
    cls = get_agent("logs")
    if not cls or not cfg.graylog.enabled:
        return {"logs_result": "", "error": "Логи недоступны для расследования."}
    agent = cls(cfg)
    ctx = state.get("db_result") or ""
    text = await agent.run(state.get("user_message") or "", context=ctx)
    return {"logs_result": text, "agents_used": ["logs"]}


async def node_inv_code(state: GraphState) -> dict:
    cfg = _app_config()
    cls = get_agent("code")
    if not cls or not cfg.gitlab.enabled:
        return {"code_result": "", "error": "Код/GitLab недоступен для расследования."}
    agent = cls(cfg)
    ctx = f"{state.get('db_result', '')}\n\n{state.get('logs_result', '')}"
    text = await agent.run(
        "По результатам БД и логов ниже: найди файлы и строки по трейсам, фрагменты кода, причину ошибки.",
        context=ctx,
    )
    return {"code_result": text, "agents_used": ["code"]}


async def node_synthesize(state: GraphState) -> dict:
    """Сводный ответ после расследования."""
    cfg = _app_config()
    if cfg.llm_status() != "ok":
        parts = [
            f"## БД\n{state.get('db_result', '')}",
            f"## Логи\n{state.get('logs_result', '')}",
            f"## Код\n{state.get('code_result', '')}",
        ]
        return {"final_response": "\n\n".join(parts), "agents_used": ["synthesize"]}

    from app.shared.llm import build_llm

    llm = build_llm(cfg)
    user_block = (
        f"Вопрос пользователя:\n{state.get('user_message', '')}\n\n"
        f"## БД\n{state.get('db_result', '')}\n\n"
        f"## Логи\n{state.get('logs_result', '')}\n\n"
        f"## Код\n{state.get('code_result', '')}"
    )
    try:
        text = await llm.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "Ты ведущий инженер поддержки. Сведи результаты этапов в один связный ответ на языке "
                        "пользователя: статус, причины, выводы. Не выдумывай факты, опирайся только на данные выше."
                    ),
                },
                {"role": "user", "content": user_block},
            ]
        )
        return {"final_response": text.strip(), "agents_used": ["synthesize"]}
    except Exception:
        log.exception("synthesize failed")
        return {
            "final_response": "\n\n".join(
                [
                    f"## БД\n{state.get('db_result', '')}",
                    f"## Логи\n{state.get('logs_result', '')}",
                    f"## Код\n{state.get('code_result', '')}",
                ]
            ),
            "agents_used": ["synthesize"],
        }


async def node_run_general(state: GraphState) -> dict:
    cfg = _app_config()
    if not cfg.general_enabled:
        return {"final_response": "Общий агент отключен (AGENT_GENERAL_ENABLED=false)."}
    cls = get_agent("general")
    if not cls:
        return {"final_response": "Общий агент недоступен."}
    agent = cls(cfg)
    text = await agent.run(state.get("user_message") or "")
    return {"final_response": text, "agents_used": ["general"]}


async def node_unknown(state: GraphState) -> dict:
    return {
        "final_response": (
            "Нет доступных агентов: включите AGENT_GENERAL_ENABLED и/или специализированных агентов "
            "(AGENT_LOGS_ENABLED / AGENT_DB_ENABLED / AGENT_CODE_ENABLED) и задайте запрос снова."
        )
    }


def route_after_router(state: GraphState) -> str:
    """Следующий узел по resolved_route (ключи path_map в graph.py)."""
    return state.get("resolved_route") or "unknown"
