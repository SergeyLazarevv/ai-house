"""Парсинг TOOL_CALL из ответа LLM. Общий для всех агентов."""

from __future__ import annotations

import json
import re


def parse_tool_call(text: str, allowed_tools: list[str]) -> tuple[str | None, dict | None]:
    """Извлекает имя инструмента и аргументы из ответа. Возвращает (name, args) или (None, None)."""
    block = re.search(r"TOOL_CALL:\s*(\w+)\s*\n?\s*(\{[\s\S]*?\})", text, re.IGNORECASE)
    if not block:
        return None, None
    name = block.group(1).strip()
    if name not in allowed_tools:
        return None, None
    try:
        args = json.loads(block.group(2))
        return name, args
    except json.JSONDecodeError:
        return None, None
