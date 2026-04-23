"""Проверка аргументов вызова инструмента (время поиска)."""

from __future__ import annotations


def tool_call_uses_default_time(name: str, args: dict | None) -> bool:
    if name not in {"search_messages", "aggregate_messages"}:
        return False
    payload = args or {}
    return "range_seconds" not in payload and "timeframe" not in payload
