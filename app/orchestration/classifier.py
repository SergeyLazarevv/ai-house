"""Классификация маршрута: keyword + опционально LLM."""

from __future__ import annotations

import json
import logging
import re

from app.config import AppConfig

log = logging.getLogger("ai_house.orchestration.classifier")

# Значения route (совпадают с path_map в graph.py)
R_LOGS = "logs"
R_DB = "db"
R_CODE = "code"
R_LOGS_CHAIN = "logs_chain"
R_INVESTIGATE = "investigate"
R_INVESTIGATE_DB_LOGS = "investigate_db_logs"
R_GENERAL = "general"
R_UNKNOWN = "unknown"


def keyword_classify(message: str) -> str:
    """Эвристика по ключевым словам; при неоднозначности см. GRAPH_ROUTER=llm."""
    msg_lower = message.lower()

    if _looks_like_investigate(msg_lower):
        return R_INVESTIGATE

    if "лог" in msg_lower or "ошибк" in msg_lower or "graylog" in msg_lower:
        if "код" in msg_lower or "трейс" in msg_lower or "файл" in msg_lower:
            return R_LOGS_CHAIN
        return R_LOGS
    if "бд" in msg_lower or "таблиц" in msg_lower or "запрос" in msg_lower or "select" in msg_lower:
        return R_DB
    if "код" in msg_lower or "gitlab" in msg_lower or "файл" in msg_lower or "репозитори" in msg_lower:
        return R_CODE
    return R_GENERAL


def _looks_like_investigate(msg_lower: str) -> bool:
    if "расслед" in msg_lower or "полный анализ" in msg_lower or "end-to-end" in msg_lower:
        return True
    if "сначала бд" in msg_lower or "бд и лог" in msg_lower or "бд, лог" in msg_lower:
        return True
    return False


async def llm_classify(message: str, config: AppConfig) -> str:
    """LLM возвращает JSON с полем route."""
    from app.shared.llm import build_llm

    if config.llm_status() != "ok":
        log.warning("LLM не настроен для маршрутизации, fallback на keyword")
        return keyword_classify(message)

    llm = build_llm(config)
    system = (
        "Ты классификатор запросов к мультиагентной системе. "
        "Ответь ТОЛЬКО одним JSON-объектом без markdown: "
        '{"route":"logs|db|code|logs_chain|investigate|investigate_db_logs|general|unknown","reason":"кратко"}\n'
        "logs — логи/Graylog/ошибки в логах без кода по трейсу.\n"
        "db — SQL, таблицы, выборки БД.\n"
        "code — GitLab, репозиторий, файл без логов.\n"
        "logs_chain — логи затем код по трейсу/файлу.\n"
        "investigate — полное расследование: БД, затем логи, затем код в репозитории.\n"
        "investigate_db_logs — только БД и логи, без GitLab.\n"
        "general — общий вопрос, приветствие, не относится к логам/БД/коду.\n"
        "unknown — нельзя сопоставить."
    )
    try:
        raw = await llm.complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": message},
            ]
        )
    except Exception:
        log.exception("LLM classify failed, fallback keyword")
        return keyword_classify(message)

    m = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
    if not m:
        return keyword_classify(message)
    try:
        data = json.loads(m.group(0))
        route = str(data.get("route", "")).strip().lower()
    except json.JSONDecodeError:
        return keyword_classify(message)

    allowed = {
        R_LOGS,
        R_DB,
        R_CODE,
        R_LOGS_CHAIN,
        R_INVESTIGATE,
        R_INVESTIGATE_DB_LOGS,
        R_GENERAL,
        R_UNKNOWN,
    }
    if route in allowed:
        return route
    return keyword_classify(message)


async def classify_route(message: str, config: AppConfig) -> str:
    if config.graph_router == "llm":
        return await llm_classify(message, config)
    return keyword_classify(message)


def _first_specialist(spec: list[str]) -> str:
    if "logs" in spec:
        return R_LOGS
    if "db" in spec:
        return R_DB
    if "code" in spec:
        return R_CODE
    return R_UNKNOWN


def resolve_route(route: str, config: AppConfig) -> str:
    """Учитывает AGENT_*_ENABLED и AGENT_GENERAL_ENABLED."""
    spec: list[str] = []
    if config.graylog.enabled:
        spec.append("logs")
    if config.postgres.enabled:
        spec.append("db")
    if config.gitlab.enabled:
        spec.append("code")

    has_general = config.general_enabled

    if not spec and not has_general:
        return R_UNKNOWN

    r = route

    if r == R_GENERAL:
        if has_general:
            return R_GENERAL
        return _first_specialist(spec)

    if r == R_INVESTIGATE:
        if {"logs", "db", "code"}.issubset(set(spec)):
            return R_INVESTIGATE
        if "db" in spec and "logs" in spec and "code" not in spec:
            return R_INVESTIGATE_DB_LOGS
        if "logs" in spec and "code" in spec:
            return R_LOGS_CHAIN
        if "db" in spec:
            return R_DB
        if "logs" in spec:
            return R_LOGS
        if "code" in spec:
            return R_CODE
        return R_GENERAL if has_general else R_UNKNOWN

    if r == R_INVESTIGATE_DB_LOGS:
        if "db" in spec and "logs" in spec:
            return R_INVESTIGATE_DB_LOGS
        if "logs" in spec:
            return R_LOGS
        if "db" in spec:
            return R_DB
        return R_GENERAL if has_general else R_UNKNOWN

    if r == R_LOGS_CHAIN and ("logs" not in spec or "code" not in spec):
        if "logs" in spec:
            return R_LOGS
        if "code" in spec:
            return R_CODE
        if "db" in spec:
            return R_DB
        return R_GENERAL if has_general else R_UNKNOWN

    if r == R_LOGS and "logs" not in spec:
        return R_DB if "db" in spec else R_CODE if "code" in spec else (R_GENERAL if has_general else R_UNKNOWN)
    if r == R_DB and "db" not in spec:
        return R_LOGS if "logs" in spec else R_CODE if "code" in spec else (R_GENERAL if has_general else R_UNKNOWN)
    if r == R_CODE and "code" not in spec:
        return R_LOGS if "logs" in spec else R_DB if "db" in spec else (R_GENERAL if has_general else R_UNKNOWN)

    if r == R_UNKNOWN:
        if has_general:
            return R_GENERAL
        return _first_specialist(spec)

    return r
