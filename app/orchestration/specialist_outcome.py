"""Классификация ответов специалистов и отпечатки делегирования (предсказуемая оркестрация).

Паттерны (общая практика мультиагентных систем, см. README «Оркестрация»):
- Наблюдения (observations) должны быть однозначными: не смешивать десятки попыток в одном тексте.
- Повторный вызов того же инструмента с тем же заданием после успеха — типичный источник бесконечных циклов (ReAct / planner loops).
- Отпечаток делегирования позволяет детерминированно запретить такой повтор, не полагаясь только на LLM.
"""

from __future__ import annotations

import hashlib
import re


def normalize_delegate_text(*parts: str) -> str:
    joined = " ".join((p or "").strip().lower() for p in parts)
    joined = re.sub(r"\s+", " ", joined).strip()
    return joined


def delegate_fingerprint(user_message: str, task: str, context_hint: str) -> str:
    """Стабильный отпечаток «намерения делегирования» для дедупликации повторных вызовов."""
    normalized = normalize_delegate_text(user_message, task, context_hint)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def looks_like_llm_policy_refusal(text: str) -> bool:
    """Короткий отказ модели/провайдера вместо фактов (не путать с данными Graylog)."""
    t = (text or "").strip()
    if not t:
        return False
    # Длинные ответы с цитатами из логов не помечаем отказом из‑за случайных фраз в message.
    if len(t) > 800:
        return False
    tl = t.lower()
    needles = (
        "я не могу обсуждать",
        "не могу обсуждать эту тему",
        "не могу обсуждать",
        "давайте поговорим о чём-нибудь ещё",
        "i cannot discuss",
        "i can't discuss",
        "i'm not able to help with that",
        "i am not able to help with that",
        "as an ai",
    )
    return any(n in tl for n in needles)


def looks_like_specialist_failure(text: str) -> bool:
    """Эвристика: ответ специалиста — сбой инструмента/конфига, а не продуктовые данные."""
    t = (text or "").strip().lower()
    if not t:
        return True
    markers = (
        "http ",
        "http 4",
        "http 5",
        "ошибка",
        "сеть / запрос",
        "агент логов:",
        "агент логов ",
        "агент бд:",
        "агент бд ",
        "агент кода:",
        "агент кода ",
        "graylog не настроен",
        "ошибка mcp",
        "mcp-сессия graylog не подключена",
        "лимит итераций",
    )
    if t.startswith(markers):
        return True
    if "не удалось" in t and ("подключ" in t or "mcp" in t or "graylog" in t or "http" in t):
        return True
    if looks_like_llm_policy_refusal(text):
        return True
    return False


# Верхняя граница вызовов одного домена за один HTTP-запрос пользователя (страховка от «пилы» оркестратора).
MAX_SPECIALIST_INVOCATIONS_PER_TURN = 2


def outcome_summary(text: str, *, max_len: int = 280) -> str:
    """Одна строка для контекста оркестратора (не полный дамп)."""
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    line = re.sub(r"\s+", " ", line).strip()
    if len(line) <= max_len:
        return line
    return line[: max_len - 1].rstrip() + "…"
