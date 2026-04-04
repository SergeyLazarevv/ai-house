"""LLM-оркестратор: на каждом шаге выбирает следующего специалиста или завершение."""

from __future__ import annotations

import json
import logging

from langgraph.config import get_config

from app.agents import get_agent
from app.config import AppConfig
from app.orchestration.agent_registry import SPECIALIST_SPECS
from app.orchestration.prompts import build_supervisor_system_prompt, summarize_state
from app.orchestration.state import GraphState
from app.shared.llm import build_llm

log = logging.getLogger("ai_house.orchestration.supervisor")

NEXT_DB = "db"
NEXT_LOGS = "logs"
NEXT_CODE = "code"
NEXT_GENERAL = "general"
NEXT_FINISH = "finish"
NEXT_END = "end"

_ALL_NEXT = {NEXT_DB, NEXT_LOGS, NEXT_CODE, NEXT_GENERAL, NEXT_FINISH, NEXT_END}


def _extract_json_object(raw: str) -> dict | None:
    i = (raw or "").find("{")
    if i < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw[i:])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _capabilities(cfg: AppConfig) -> dict[str, bool]:
    enabled_flags = {
        "db": cfg.postgres.enabled,
        "logs": cfg.graylog.enabled,
        "code": cfg.gitlab.enabled,
        "general": cfg.general_enabled,
    }
    return {spec.role: bool(enabled_flags.get(spec.role) and get_agent(spec.role)) for spec in SPECIALIST_SPECS}


def _allowed_list(cap: dict[str, bool]) -> list[str]:
    return [k for k in (NEXT_DB, NEXT_LOGS, NEXT_CODE, NEXT_GENERAL) if cap.get(k)]


def _validate_next(raw: str, cap: dict[str, bool]) -> str:
    n = (raw or "").strip().lower()
    if n not in _ALL_NEXT:
        return NEXT_FINISH
    if n in (NEXT_FINISH, NEXT_END):
        return n
    if not cap.get(n):
        return NEXT_FINISH
    return n


async def node_supervisor(state: GraphState) -> dict:
    cfg = get_config()["configurable"]["app_config"]
    assert isinstance(cfg, AppConfig)

    step = int(state.get("supervisor_step") or 0) + 1
    max_steps = max(1, cfg.graph_supervisor_max_steps)
    cap = _capabilities(cfg)
    allowed = _allowed_list(cap)

    if not allowed and not cap[NEXT_GENERAL]:
        return {
            "supervisor_step": step,
            "supervisor_next": NEXT_END,
            "final_response": (
                "Нет доступных агентов: включите AGENT_GENERAL_ENABLED и/или "
                "AGENT_LOGS_ENABLED / AGENT_DB_ENABLED / AGENT_CODE_ENABLED."
            ),
        }

    if step > max_steps:
        log.warning("[%s] supervisor max_steps=%s", state.get("trace_id", ""), max_steps)
        return {
            "supervisor_step": step,
            "supervisor_next": NEXT_FINISH,
            "supervisor_reason": "max_steps",
        }

    msg = state.get("user_message") or ""

    if cfg.llm_status() != "ok":
        return {
            "supervisor_step": step,
            "supervisor_next": NEXT_END,
            "final_response": (
                "LLM оркестратора не настроен. Проверьте настройки провайдера "
                "(например, YANDEX_API_KEY / YANDEX_CATALOG_ID, OPENAI_API_KEY или ANTHROPIC_API_KEY)."
            ),
        }

    llm = build_llm(cfg)
    parts = list(allowed)
    if cap[NEXT_GENERAL] and NEXT_GENERAL not in parts:
        parts.append(NEXT_GENERAL)
    system = build_supervisor_system_prompt(parts if parts else [NEXT_GENERAL])
    user_block = (
        f"Вопрос пользователя:\n{msg}\n\n"
        f"Уже вызывали (порядок): {state.get('agents_used') or []}\n\n"
        f"{summarize_state(state)}"
    )

    try:
        raw = await llm.complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user_block},
            ]
        )
    except Exception:
        log.exception("supervisor LLM failed")
        return {
            "supervisor_step": step,
            "supervisor_next": NEXT_FINISH,
            "supervisor_reason": "llm_error",
        }

    data = _extract_json_object(raw) or {}
    nxt = str(data.get("next", "finish")).strip().lower()
    reason = str(data.get("reason", "")).strip()
    task = str(data.get("task", "")).strip()
    hint = str(data.get("context_hint", "")).strip()

    nxt = _validate_next(nxt, cap)
    if nxt == NEXT_GENERAL and not cap[NEXT_GENERAL]:
        nxt = NEXT_FINISH

    if nxt not in (NEXT_FINISH, NEXT_END):
        if not task:
            task = msg
            log.warning("[%s] supervisor empty task, fallback to user_message", state.get("trace_id", ""))

    out: dict = {
        "supervisor_step": step,
        "supervisor_next": nxt,
        "supervisor_reason": reason,
        "supervisor_task": "" if nxt in (NEXT_FINISH, NEXT_END) else task,
        "supervisor_context_hint": "" if nxt in (NEXT_FINISH, NEXT_END) else hint,
    }
    return out


def route_after_supervisor(state: GraphState) -> str:
    return state.get("supervisor_next") or NEXT_END


def route_after_sup_agent(state: GraphState) -> str:
    return "supervisor"
