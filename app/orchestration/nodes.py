"""Узлы графа: специалисты в цикле под управлением supervisor и финальный синтез."""

from __future__ import annotations

import logging
from collections.abc import Callable

from langgraph.config import get_config

from app.agents import get_agent
from app.config import AppConfig
from app.orchestration.agent_registry import AgentSpec, SPECIALIST_BY_ROLE
from app.orchestration.prompts import build_synthesize_system_prompt
from app.orchestration.state import GraphState

log = logging.getLogger("ai_house.orchestration.nodes")


def _app_config() -> AppConfig:
    return get_config()["configurable"]["app_config"]


def _merge_slot(prev: str | None, new: str) -> str:
    p = (prev or "").strip()
    n = (new or "").strip()
    if not p:
        return n
    if not n:
        return p
    return f"{p}\n\n--- этап ---\n\n{n}"


def _specialist_inputs(state: GraphState) -> tuple[str, str]:
    """Сообщение и контекст для агента: только задание оркестратора и его краткая выжимка."""
    task = (state.get("supervisor_task") or "").strip()
    if not task:
        task = (state.get("user_message") or "").strip()
    hint = (state.get("supervisor_context_hint") or "").strip()
    return task, hint


def _clear_supervisor_task() -> dict:
    return {"supervisor_task": "", "supervisor_context_hint": ""}


async def _run_specialist(state: GraphState, spec: AgentSpec) -> dict:
    cfg = _app_config()
    msg, ctx = _specialist_inputs(state)
    enabled_flags = {
        "db": cfg.postgres.enabled,
        "logs": cfg.graylog.enabled,
        "code": cfg.gitlab.enabled,
        "general": cfg.general_enabled,
    }
    if not enabled_flags.get(spec.role):
        text = spec.disabled_message
        return {
            spec.result_slot: _merge_slot(state.get(spec.result_slot), text),
            "agents_used": [spec.role],
            **_clear_supervisor_task(),
        }
    cls = get_agent(spec.role)
    if not cls:
        text = spec.unavailable_message
        return {
            spec.result_slot: _merge_slot(state.get(spec.result_slot), text),
            "agents_used": [spec.role],
            **_clear_supervisor_task(),
        }
    text = await cls(cfg).run(msg, context=ctx)
    log.info("[%s] %s done", state.get("trace_id", ""), spec.node_name)
    return {
        spec.result_slot: _merge_slot(state.get(spec.result_slot), text),
        "agents_used": [spec.role],
        **_clear_supervisor_task(),
    }


def make_specialist_node(role: str) -> Callable[[GraphState], dict]:
    spec = SPECIALIST_BY_ROLE[role]

    async def _node(state: GraphState) -> dict:
        return await _run_specialist(state, spec)

    return _node


async def node_synthesize(state: GraphState) -> dict:
    """Сводный ответ по результатам специалистов (порядок вызовов задавал оркестратор)."""
    cfg = _app_config()
    gen = (state.get("final_response") or "").strip()
    if cfg.llm_status() != "ok":
        parts = [
            f"## БД\n{state.get('db_result', '')}",
            f"## Логи\n{state.get('logs_result', '')}",
            f"## Код\n{state.get('code_result', '')}",
        ]
        if gen:
            parts.append(f"## Общий агент\n{gen}")
        return {"final_response": "\n\n".join(parts), "agents_used": ["synthesize"]}

    from app.shared.llm import build_llm

    llm = build_llm(cfg)
    user_block = (
        f"Вопрос пользователя:\n{state.get('user_message', '')}\n\n"
        f"## БД\n{state.get('db_result', '')}\n\n"
        f"## Логи\n{state.get('logs_result', '')}\n\n"
        f"## Код\n{state.get('code_result', '')}\n\n"
        f"## Общий агент (если вызывался)\n{gen}"
    )
    try:
        text = await llm.complete(
            [
                {
                    "role": "system",
                    "content": build_synthesize_system_prompt(),
                },
                {"role": "user", "content": user_block},
            ]
        )
        return {"final_response": text.strip(), "agents_used": ["synthesize"]}
    except Exception:
        log.exception("synthesize failed")
        parts = [
            f"## БД\n{state.get('db_result', '')}",
            f"## Логи\n{state.get('logs_result', '')}",
            f"## Код\n{state.get('code_result', '')}",
        ]
        if gen:
            parts.append(f"## Общий агент\n{gen}")
        return {"final_response": "\n\n".join(parts), "agents_used": ["synthesize"]}
