"""LLM-оркестратор: на каждом шаге выбирает следующего специалиста или завершение."""

from __future__ import annotations

import json
import logging
import re

from langgraph.config import get_config

from app.agents import get_agent
from app.config import AppConfig
from app.orchestration.agent_registry import SPECIALIST_SPECS
from app.orchestration.prompts import (
    build_orchestrator_direct_answer_prompt,
    build_supervisor_system_prompt,
    summarize_state,
)
from app.orchestration.specialist_outcome import (
    MAX_SPECIALIST_INVOCATIONS_PER_TURN,
    delegate_fingerprint,
)
from app.orchestration.state import GraphState
from app.shared.llm import build_llm

log = logging.getLogger("ai_house.orchestration.supervisor")

NEXT_DB = "db"
NEXT_LOGS = "logs"
NEXT_CODE = "code"
NEXT_FINISH = "finish"
NEXT_END = "end"

_ALL_NEXT = {NEXT_DB, NEXT_LOGS, NEXT_CODE, NEXT_FINISH, NEXT_END}


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
    }
    return {spec.role: bool(enabled_flags.get(spec.role) and get_agent(spec.role)) for spec in SPECIALIST_SPECS}


def _allowed_list(cap: dict[str, bool]) -> list[str]:
    return [k for k in (NEXT_DB, NEXT_LOGS, NEXT_CODE) if cap.get(k)]


def _validate_next(raw: str, cap: dict[str, bool]) -> str:
    n = (raw or "").strip().lower()
    if n not in _ALL_NEXT:
        return NEXT_FINISH
    if n in (NEXT_FINISH, NEXT_END):
        return n
    if not cap.get(n):
        return NEXT_FINISH
    return n


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"true", "1", "yes", "y", "да"}


def _as_confidence(value: object) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, score))


def _normalize_answer_mode(value: object) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in {"direct", "delegate"} else "direct"


def _normalize_target_domain(value: object, cap: dict[str, bool]) -> str:
    domain = str(value or "").strip().lower()
    if domain not in {NEXT_DB, NEXT_LOGS, NEXT_CODE, "none"}:
        return "none"
    if domain in {NEXT_DB, NEXT_LOGS, NEXT_CODE} and not cap.get(domain):
        return "none"
    return domain


def _has_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _detect_explicit_domain_request(msg: str, cap: dict[str, bool]) -> str | None:
    text = (msg or "").strip()
    if not text:
        return None

    explicit_ask = (
        r"\b(спроси|посмотри|проверь|покажи|найди|назови|перечисли|скажи|дай)\b",
        r"\b(какие|какой|какая|какое|сколько|есть ли)\b",
    )
    asks_agent = r"\b(агент[а-я]*|специалист[а-я]*)\b"

    if cap.get(NEXT_LOGS):
        logs_source = (
            r"\bgraylog\b",
            r"\bгрейлог[аеу]?\b",
            r"\bлог[аиовех]*\b",
            r"\binputs?\b",
            r"\bstreams?\b",
            r"\bинпут[а-я]*\b",
            r"\bстрим[а-я]*\b",
            r"\binhouse[\w-]*\b",
        )
        asks_logs_agent = _has_any(text, explicit_ask) and (
            _has_any(text, logs_source)
            or (re.search(asks_agent, text, re.IGNORECASE) and re.search(r"\b(graylog|грейлог|лог[аиовех]*)\b", text, re.IGNORECASE))
        )
        if asks_logs_agent:
            return NEXT_LOGS

    if cap.get(NEXT_DB):
        db_source = (
            r"\bбд\b",
            r"\bdatabase\b",
            r"\bpostgres(?:ql)?\b",
            r"\bтаблиц[а-я]*\b",
            r"\bзапис[а-я]*\b",
        )
        asks_db_agent = _has_any(text, explicit_ask) and (
            _has_any(text, db_source)
            or (re.search(asks_agent, text, re.IGNORECASE) and re.search(r"\b(db|database|бд|postgres(?:ql)?)\b", text, re.IGNORECASE))
        )
        if asks_db_agent:
            return NEXT_DB

    if cap.get(NEXT_CODE):
        code_source = (
            r"\bкод[аеуы]?\b",
            r"\bрепозитори[йя]\b",
            r"\bgitlab\b",
            r"\bконфиг[а-я]*\b",
            r"\bфайл[а-я]*\b",
        )
        asks_code_agent = _has_any(text, explicit_ask) and (
            _has_any(text, code_source)
            or (re.search(asks_agent, text, re.IGNORECASE) and re.search(r"\b(code|gitlab|код[аеуы]?)\b", text, re.IGNORECASE))
        )
        if asks_code_agent:
            return NEXT_CODE

    return None


def _success_fingerprint_key(domain: str) -> str | None:
    return {
        NEXT_LOGS: "logs_success_fingerprint",
        NEXT_DB: "db_success_fingerprint",
        NEXT_CODE: "code_success_fingerprint",
    }.get(domain)


def _invocation_state_key(domain: str) -> str | None:
    return {
        NEXT_LOGS: "logs_invocations",
        NEXT_DB: "db_invocations",
        NEXT_CODE: "code_invocations",
    }.get(domain)


def _orchestration_hints(state: GraphState) -> str:
    """Краткие системные подсказки: снижают вероятность повторного delegate после успеха (LLM+код)."""
    lines: list[str] = []
    if state.get("logs_success_fingerprint"):
        lines.append(
            "Логи: в этом запросе уже был успешный ответ специалиста для сохранённой формулировки задачи. "
            "Не вызывай logs снова с тем же смыслом задачи — выбирай finish и передай сводке."
        )
    if state.get("db_success_fingerprint"):
        lines.append(
            "БД: уже был успешный ответ для сохранённой задачи; не повторяй db с тем же смыслом без новой причины."
        )
    if state.get("code_success_fingerprint"):
        lines.append(
            "Код: уже был успешный ответ для сохранённой задачи; не повторяй code с тем же смыслом без новой причины."
        )
    if not lines:
        return ""
    return "\n\n## Системные инварианты (не игнорируй)\n" + "\n".join(f"- {line}" for line in lines)


def _coerce_supervisor_decision(
    *,
    msg: str,
    cap: dict[str, bool],
    has_specialist_results: bool,
    raw_next: str,
    raw_answer_mode: str,
    raw_target_domain: str,
    raw_needs_external_data: bool,
    raw_user_explicitly_requested_source: bool,
) -> tuple[str, str, str, bool, bool]:
    forced_domain = _detect_explicit_domain_request(msg, cap) if not has_specialist_results else None
    nxt = _validate_next(raw_next, cap)
    answer_mode = _normalize_answer_mode(raw_answer_mode)
    target_domain = _normalize_target_domain(raw_target_domain, cap)
    needs_external_data = raw_needs_external_data
    user_explicitly_requested_source = raw_user_explicitly_requested_source

    # Контракт: если модель выбрала finish/end, это приоритетнее target_domain/answer_mode.
    if nxt in {NEXT_FINISH, NEXT_END}:
        return nxt, "direct", "none", False, user_explicitly_requested_source

    if forced_domain:
        user_explicitly_requested_source = True
        needs_external_data = True
        answer_mode = "delegate"
        target_domain = forced_domain
        nxt = forced_domain

    if nxt in {NEXT_DB, NEXT_LOGS, NEXT_CODE}:
        answer_mode = "delegate"
        target_domain = nxt
        needs_external_data = True
    elif answer_mode == "delegate" and target_domain in {NEXT_DB, NEXT_LOGS, NEXT_CODE}:
        nxt = target_domain
        needs_external_data = True
    else:
        answer_mode = "direct"
        target_domain = "none"
        nxt = NEXT_FINISH

    if nxt == NEXT_FINISH:
        target_domain = "none"

    return nxt, answer_mode, target_domain, needs_external_data, user_explicitly_requested_source


async def node_supervisor(state: GraphState) -> dict:
    cfg = get_config()["configurable"]["app_config"]
    assert isinstance(cfg, AppConfig)

    step = int(state.get("supervisor_step") or 0) + 1
    max_steps = max(1, cfg.graph_supervisor_max_steps)
    cap = _capabilities(cfg)
    allowed = _allowed_list(cap)

    if step > max_steps:
        log.warning("[%s] supervisor max_steps=%s", state.get("trace_id", ""), max_steps)
        return {
            "supervisor_step": step,
            "supervisor_next": NEXT_FINISH,
            "supervisor_reason": "max_steps",
            "supervisor_truncated": True,
        }

    msg = state.get("user_message") or ""
    last_status = (state.get("last_specialist_status") or "none").strip().lower()
    last_role = (state.get("last_specialist_role") or "").strip()
    last_error = (state.get("last_specialist_error") or "").strip()

    # Единый контракт: если последний вызов специалиста завершился ошибкой, оркестратор
    # сразу завершает запрос и возвращает реальную причину без повторных попыток.
    if step > 1 and last_status == "error":
        reason = last_error or f"Ошибка специалиста `{last_role or 'unknown'}`."
        return {
            "supervisor_step": step,
            "supervisor_next": NEXT_END,
            "supervisor_reason": "specialist_error",
            "final_response": (
                "Не удалось выполнить запрос: сервис специалиста завершился ошибкой.\n"
                f"Причина: {reason}"
            ),
            "agents_used": ["orchestrator"],
            "supervisor_task": "",
            "supervisor_context_hint": "",
        }

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
    system = build_supervisor_system_prompt(allowed)
    user_block = (
        f"Вопрос пользователя:\n{msg}\n\n"
        f"Уже вызывали (порядок): {state.get('agents_used') or []}\n\n"
        f"{summarize_state(state)}"
        f"{_orchestration_hints(state)}"
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
            "supervisor_next": NEXT_END,
            "supervisor_reason": "llm_error",
            "final_response": (
                "Сервис оркестратора недоступен: не удалось получить решение от LLM supervisor. "
                "Повторите запрос позже."
            ),
            "agents_used": ["orchestrator"],
        }

    data = _extract_json_object(raw) or {}
    nxt = str(data.get("next", "finish")).strip().lower()
    reason = str(data.get("reason", "")).strip()
    task = str(data.get("task", "")).strip()
    hint = str(data.get("context_hint", "")).strip()
    answer_mode = _normalize_answer_mode(data.get("answer_mode"))
    target_domain = _normalize_target_domain(data.get("target_domain"), cap)
    needs_external_data = _as_bool(data.get("needs_external_data"))
    user_explicitly_requested_source = _as_bool(data.get("user_explicitly_requested_source"))
    confidence = _as_confidence(data.get("confidence"))

    has_specialist_results = any(
        (state.get(slot) or "").strip() for slot in ("db_result", "logs_result", "code_result")
    )
    nxt, answer_mode, target_domain, needs_external_data, user_explicitly_requested_source = _coerce_supervisor_decision(
        msg=msg,
        cap=cap,
        has_specialist_results=has_specialist_results,
        raw_next=nxt,
        raw_answer_mode=answer_mode,
        raw_target_domain=target_domain,
        raw_needs_external_data=needs_external_data,
        raw_user_explicitly_requested_source=user_explicitly_requested_source,
    )

    if nxt in (NEXT_DB, NEXT_LOGS, NEXT_CODE) and not task:
        log.warning("[%s] supervisor empty task for delegate -> end", state.get("trace_id", ""))
        return {
            "supervisor_step": step,
            "supervisor_next": NEXT_END,
            "supervisor_reason": "invalid_delegate_task",
            "supervisor_answer_mode": "direct",
            "supervisor_target_domain": "none",
            "supervisor_needs_external_data": False,
            "supervisor_user_explicitly_requested_source": user_explicitly_requested_source,
            "supervisor_confidence": confidence,
            "supervisor_task": "",
            "supervisor_context_hint": "",
            "final_response": (
                "Сервис оркестратора вернул некорректный маршрут: пустое задание для специалиста. "
                "Запрос не был отправлен повторно, чтобы избежать ложных результатов."
            ),
            "agents_used": ["orchestrator"],
        }

    if nxt in (NEXT_DB, NEXT_LOGS, NEXT_CODE):
        inv_key = _invocation_state_key(nxt)
        prev_inv = int(state.get(inv_key) or 0) if inv_key else 0
        if prev_inv >= MAX_SPECIALIST_INVOCATIONS_PER_TURN:
            log.warning(
                "[%s] supervisor: %s invocations >= %s -> finish",
                state.get("trace_id", ""),
                nxt,
                MAX_SPECIALIST_INVOCATIONS_PER_TURN,
            )
            nxt = NEXT_FINISH
            answer_mode = "direct"
            target_domain = "none"
            needs_external_data = False
            reason = f"{reason} [policy: specialist_invocation_cap]".strip() if reason else "[policy: specialist_invocation_cap]"
        else:
            fp_key = _success_fingerprint_key(nxt)
            if fp_key:
                cand = delegate_fingerprint(msg, task, hint)
                if cand and (state.get(fp_key) or "") == cand:
                    log.info(
                        "[%s] supervisor: duplicate delegate to %s (successful fingerprint) -> finish",
                        state.get("trace_id", ""),
                        nxt,
                    )
                    nxt = NEXT_FINISH
                    answer_mode = "direct"
                    target_domain = "none"
                    needs_external_data = False
                    reason = f"{reason} [policy: delegate_dedup]".strip() if reason else "[policy: delegate_dedup]"

    if nxt == NEXT_FINISH and not has_specialist_results:
        try:
            direct = await llm.complete(
                [
                    {
                        "role": "system",
                        "content": build_orchestrator_direct_answer_prompt(allowed),
                    },
                    {"role": "user", "content": msg},
                ]
            )
        except Exception:
            log.exception("supervisor direct answer failed")
            direct = "Не удалось сформировать прямой ответ оркестратора."
        return {
            "supervisor_step": step,
            "supervisor_next": NEXT_END,
            "supervisor_reason": reason,
            "supervisor_answer_mode": answer_mode,
            "supervisor_target_domain": target_domain,
            "supervisor_needs_external_data": needs_external_data,
            "supervisor_user_explicitly_requested_source": user_explicitly_requested_source,
            "supervisor_confidence": confidence,
            "supervisor_task": "",
            "supervisor_context_hint": "",
            "final_response": (direct or "").strip(),
            "agents_used": ["orchestrator"],
        }

    out: dict = {
        "supervisor_step": step,
        "supervisor_next": nxt,
        "supervisor_reason": reason,
        "supervisor_answer_mode": answer_mode,
        "supervisor_target_domain": target_domain,
        "supervisor_needs_external_data": needs_external_data,
        "supervisor_user_explicitly_requested_source": user_explicitly_requested_source,
        "supervisor_confidence": confidence,
        "supervisor_task": "" if nxt in (NEXT_FINISH, NEXT_END) else task,
        "supervisor_context_hint": "" if nxt in (NEXT_FINISH, NEXT_END) else hint,
    }
    return out


def route_after_supervisor(state: GraphState) -> str:
    return state.get("supervisor_next") or NEXT_END


def route_after_sup_agent(state: GraphState) -> str:
    return "supervisor"
