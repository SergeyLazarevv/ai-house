"""Обрезка ответов инструментов и метки времени по умолчанию."""

from __future__ import annotations

import json

MAX_TOOL_RESULT_CHARS = 24_000
DEFAULT_TIME_RANGE_SECONDS = 300
_DEFAULT_SLIM_CTXT = 900
_DEFAULT_SLIM_MESSAGE = 2_000


def is_tool_error(text: str) -> bool:
    t = (text or "").strip().lower()
    error_markers = (
        "http ",
        "ошибка",
        "сеть / запрос",
        "агент логов:",
        "ошибка mcp",
        "graylog не настроен",
        "mcp-сессия graylog не подключена",
    )
    # Не считаем произвольную фразу "не удалось" признаком сбоя инструмента:
    # она часто встречается в реальных продуктовых сообщениях логов.
    return t.startswith(error_markers)


def _slim_search_messages_payload(data: dict) -> dict | None:
    """Урезает тяжёлые поля в samples (меньше риск отказа провайдера LLM на stack trace)."""
    msgs = data.get("messages")
    if not isinstance(msgs, list):
        return None
    changed = False
    heavy_str_keys = (
        "ctxt_exception",
        "full_message",
        "message",
        "stack_trace",
    )
    for item in msgs:
        if not isinstance(item, dict):
            continue
        for key in heavy_str_keys:
            val = item.get(key)
            if not isinstance(val, str):
                continue
            limit = _DEFAULT_SLIM_CTXT if key == "ctxt_exception" else _DEFAULT_SLIM_MESSAGE
            if len(val) > limit:
                item[key] = val[: limit - 30] + "\n… [обрезано для LLM]"
                changed = True
    return data if changed else None


def slim_graylog_tool_json_for_llm(raw: str) -> str | None:
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    slimmed = _slim_search_messages_payload(data)
    if slimmed is None:
        return None
    return json.dumps(slimmed, ensure_ascii=False)


def clip_tool_result_for_llm(text: str) -> str:
    slim = slim_graylog_tool_json_for_llm(text)
    body = slim if slim is not None else text
    if len(body) <= MAX_TOOL_RESULT_CHARS:
        return body
    head = MAX_TOOL_RESULT_CHARS - 200
    return (
        body[:head]
        + "\n\n… [ответ инструмента обрезан: слишком много данных для LLM. "
        "Используй response_shape=count, aggregate_messages или меньший limit.]\n"
    )


def search_messages_empty_likely_placeholder_args(raw: str) -> bool:
    """True если search_messages вернул 0 записей и query похож на шаблон с <...>."""
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    total = data.get("total_results")
    try:
        n = int(total) if total is not None else -1
    except (TypeError, ValueError):
        return False
    if n != 0:
        return False
    msgs = data.get("messages")
    if isinstance(msgs, list) and len(msgs) > 0:
        return False
    q = str(data.get("query") or "")
    return "<" in q and ">" in q


def extract_range_seconds(text: str) -> int | None:
    try:
        data = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("range_seconds")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def humanize_range_seconds(seconds: int) -> str:
    if seconds % 604800 == 0:
        weeks = seconds // 604800
        return f"последние {weeks} нед." if weeks > 1 else "последнюю неделю"
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"последние {days} дн." if days > 1 else "последние сутки"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"последние {hours} ч." if hours > 1 else "последний час"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"последние {minutes} мин."
    return f"последние {seconds} сек."


def with_default_time_note(text: str, seconds: int | None) -> str:
    if not text:
        return text
    used_seconds = seconds or DEFAULT_TIME_RANGE_SECONDS
    note = (
        f"- Время в запросе не было указано, поэтому использовано окно по умолчанию: "
        f"{humanize_range_seconds(used_seconds)}."
    )
    marker = "2. Факты:\n"
    if marker in text and note not in text:
        return text.replace(marker, f"{marker}{note}\n", 1)
    if note in text:
        return text
    suffix = (
        "\n\nПримечание: время в запросе не было указано, "
        f"поэтому использовано окно по умолчанию: {humanize_range_seconds(used_seconds)}."
    )
    return text.rstrip() + suffix
