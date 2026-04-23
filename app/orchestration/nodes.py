"""Узлы графа: специалисты в цикле под управлением supervisor и финальный синтез."""

from __future__ import annotations

import logging
from collections.abc import Callable

from langgraph.config import get_config

from app.agents import get_agent
from app.config import AppConfig
from app.orchestration.agent_registry import AgentSpec, SPECIALIST_BY_ROLE
from app.orchestration.prompts import build_synthesize_system_prompt
from app.orchestration.specialist_outcome import (
    delegate_fingerprint,
    looks_like_llm_policy_refusal,
    looks_like_specialist_failure,
    outcome_summary,
)
from app.orchestration.state import GraphState

log = logging.getLogger("ai_house.orchestration.nodes")

_FP_KEY = {"logs": "logs_success_fingerprint", "db": "db_success_fingerprint", "code": "code_success_fingerprint"}
_INV_KEY = {"logs": "logs_invocations", "db": "db_invocations", "code": "code_invocations"}


def _looks_like_empty_synthesis(text: str) -> bool:
    normalized = (text or "").strip()
    if not normalized:
        return True
    compact = "".join(normalized.split())
    return compact in {"{}", "```{}```", "```json{}```"}


def _app_config() -> AppConfig:
    return get_config()["configurable"]["app_config"]


def _specialist_inputs(state: GraphState) -> tuple[str, str]:
    """Сообщение и контекст для агента: задание оркестратора и краткая выжимка."""
    task = (state.get("supervisor_task") or "").strip()
    if not task:
        task = (state.get("user_message") or "").strip()
    hint = (state.get("supervisor_context_hint") or "").strip()
    # Исходный вопрос нужен даже при нетривиальном task: оркестратор может сократить формулировку
    # и потерять ключевые слова («топ N по частоте» и т.д.), от которых зависят эвристики агентов.
    original = (state.get("user_message") or "").strip()
    if original and original != task:
        sep = "\n\n--- исходный вопрос пользователя ---\n"
        hint = f"{hint}{sep}{original}".strip() if hint else f"--- исходный вопрос пользователя ---\n{original}"
    return task, hint


def _clear_supervisor_task() -> dict:
    return {"supervisor_task": "", "supervisor_context_hint": ""}


def _deterministic_error_response(logs_text: str) -> str:
    text = (logs_text or "").strip()
    if not text:
        facts = ["Не удалось получить данные из логов."]
    else:
        facts = [text[:2000]] if len(text) <= 2000 else [text[:1980] + "…"]
    bullets = "\n".join(f"- {fact}" for fact in facts[:5])
    return (
        "1. Кратко: не удалось получить итоговые данные из логов.\n"
        "2. Факты:\n"
        f"{bullets}\n"
        "3. Вывод: исправьте ошибку инструмента или маршрута Graylog и повторите запрос."
    )


async def _run_specialist(state: GraphState, spec: AgentSpec) -> dict:
    cfg = _app_config()
    msg, ctx = _specialist_inputs(state)
    fp_key = _FP_KEY[spec.role]
    inv_key = _INV_KEY[spec.role]
    enabled_flags = {
        "db": cfg.postgres.enabled,
        "logs": cfg.graylog.enabled,
        "code": cfg.gitlab.enabled,
    }
    if not enabled_flags.get(spec.role):
        text = spec.disabled_message
        return {
            spec.result_slot: text,
            fp_key: "",
            inv_key: 1,
            "last_specialist_role": spec.role,
            "last_specialist_status": "error",
            "last_specialist_error": text,
            "agents_used": [spec.role],
            **_clear_supervisor_task(),
        }
    cls = get_agent(spec.role)
    if not cls:
        text = spec.unavailable_message
        return {
            spec.result_slot: text,
            fp_key: "",
            inv_key: 1,
            "last_specialist_role": spec.role,
            "last_specialist_status": "error",
            "last_specialist_error": text,
            "agents_used": [spec.role],
            **_clear_supervisor_task(),
        }
    user_q = (state.get("user_message") or "").strip()
    fp = delegate_fingerprint(user_q, msg, ctx)
    text = await cls(cfg).run(msg, context=ctx)
    if spec.role == "logs" and looks_like_llm_policy_refusal(text):
        text = (
            "Агент логов: провайдер LLM отказался сформулировать ответ по данным Graylog "
            "(часто из‑за объёма stack trace и полей при `fields=full`). "
            "Инструменты при этом могли отработать успешно — повторите запрос или сузьте выборку."
        ).strip()
    log.info("[%s] %s done", state.get("trace_id", ""), spec.node_name)
    ok = not looks_like_specialist_failure(text)
    out: dict = {
        spec.result_slot: text,
        inv_key: 1,
        "last_specialist_role": spec.role,
        "agents_used": [spec.role],
        **_clear_supervisor_task(),
    }
    if ok:
        out[fp_key] = fp
        out["last_specialist_status"] = "success"
        out["last_specialist_error"] = ""
        log.info(
            "[%s] %s success fingerprint=%s summary=%r",
            state.get("trace_id", ""),
            spec.role,
            fp[:12],
            outcome_summary(text)[:80],
        )
    else:
        out[fp_key] = ""
        out["last_specialist_status"] = "error"
        out["last_specialist_error"] = (text or "").strip()
    return out


def make_specialist_node(role: str) -> Callable[[GraphState], dict]:
    spec = SPECIALIST_BY_ROLE[role]

    async def _node(state: GraphState) -> dict:
        return await _run_specialist(state, spec)

    return _node


async def node_synthesize(state: GraphState) -> dict:
    """Сводный ответ по результатам специалистов (порядок вызовов задавал оркестратор)."""
    cfg = _app_config()
    gen = (state.get("final_response") or "").strip()
    logs_text = (state.get("logs_result") or "").strip()
    truncated = bool(state.get("supervisor_truncated"))
    if looks_like_specialist_failure(logs_text):
        body = _deterministic_error_response(logs_text)
        if truncated:
            body = (
                "Достигнут лимит шагов оркестратора; ниже последний зафиксированный результат по логам.\n\n" + body
            )
        return {
            "final_response": body,
            "agents_used": ["synthesize"],
        }
    if cfg.llm_status() != "ok":
        parts = [
            f"## БД\n{state.get('db_result', '')}",
            f"## Логи\n{state.get('logs_result', '')}",
            f"## Код\n{state.get('code_result', '')}",
        ]
        if gen:
            parts.append(f"## Оркестратор\n{gen}")
        return {"final_response": "\n\n".join(parts), "agents_used": ["synthesize"]}

    from app.shared.llm import build_llm

    llm = build_llm(cfg)
    trunc_note = ""
    if truncated:
        trunc_note = (
            "\n\n[Система] Достигнут лимит шагов оркестратора (GRAPH_SUPERVISOR_MAX_STEPS). "
            "Сведи ответ из последних результатов ниже; не требуй новых вызовов специалистов.\n"
        )
    user_block = (
        f"Вопрос пользователя:\n{state.get('user_message', '')}\n\n"
        f"## БД\n{state.get('db_result', '')}\n\n"
        f"## Логи\n{state.get('logs_result', '')}\n\n"
        f"## Код\n{state.get('code_result', '')}\n\n"
        f"## Оркестратор (если отвечал напрямую)\n{gen}"
        f"{trunc_note}"
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
        final_text = (text or "").strip()
        if _looks_like_empty_synthesis(final_text):
            log.warning(
                "[%s] synthesize returned empty placeholder; fallback to deterministic sections",
                state.get("trace_id", ""),
            )
            parts = [
                f"## БД\n{state.get('db_result', '')}",
                f"## Логи\n{state.get('logs_result', '')}",
                f"## Код\n{state.get('code_result', '')}",
            ]
            if gen:
                parts.append(f"## Оркестратор\n{gen}")
            return {"final_response": "\n\n".join(parts), "agents_used": ["synthesize"]}
        return {"final_response": final_text, "agents_used": ["synthesize"]}
    except Exception:
        log.exception("synthesize failed")
        parts = [
            f"## БД\n{state.get('db_result', '')}",
            f"## Логи\n{state.get('logs_result', '')}",
            f"## Код\n{state.get('code_result', '')}",
        ]
        if gen:
            parts.append(f"## Оркестратор\n{gen}")
        return {"final_response": "\n\n".join(parts), "agents_used": ["synthesize"]}
