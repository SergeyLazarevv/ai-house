"""Парсинг TOOL_CALL из ответа LLM. Общий для всех агентов."""

from __future__ import annotations

import json
import re


def parse_all_tool_calls(text: str, allowed_tools: list[str]) -> list[tuple[str, dict]]:
    """Все вызовы TOOL_CALL по порядку (если модель вернула несколько в одном сообщении)."""
    if not text:
        return []
    allowed = set(allowed_tools)
    out: list[tuple[str, dict]] = []
    pos = 0
    while pos < len(text):
        m = re.search(r"TOOL_CALL:\s*(\w+)", text[pos:], re.IGNORECASE)
        if not m:
            break
        name = m.group(1).strip()
        after_call = pos + m.end()
        sub = text[after_call:]
        brace_idx = sub.find("{")
        if brace_idx < 0:
            pos = after_call
            continue
        if name not in allowed:
            pos = after_call + brace_idx + 1
            continue
        raw_json = sub[brace_idx:]
        try:
            args, used = json.JSONDecoder().raw_decode(raw_json)
        except json.JSONDecodeError:
            pos = after_call + brace_idx + 1
            continue
        if not isinstance(args, dict):
            pos = after_call + brace_idx + 1
            continue
        out.append((name, args))
        pos = after_call + brace_idx + used
    return out


def parse_tool_call(text: str, allowed_tools: list[str]) -> tuple[str | None, dict | None]:
    """Извлекает имя инструмента и аргументы из ответа. Возвращает (name, args) или (None, None)."""
    head = re.search(r"TOOL_CALL:\s*(\w+)", text or "", re.IGNORECASE)
    if not head:
        return None, None
    name = head.group(1).strip()
    if name not in allowed_tools:
        return None, None
    tail = (text or "")[head.end() :]
    brace_idx = tail.find("{")
    if brace_idx < 0:
        return None, None
    raw_json = tail[brace_idx:]
    try:
        args, _ = json.JSONDecoder().raw_decode(raw_json)
        if not isinstance(args, dict):
            return None, None
        return name, args
    except json.JSONDecodeError:
        return None, None
