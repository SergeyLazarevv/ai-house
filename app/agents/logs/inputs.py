"""Сопоставление запроса пользователя с Graylog input (gl2_source_input)."""

from __future__ import annotations

import json
import re


def extract_inputs_catalog(text: str) -> list[dict[str, str]]:
    try:
        data = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    raw_inputs = data.get("inputs")
    if not isinstance(raw_inputs, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw_inputs:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        input_id = str(item.get("id") or "").strip()
        if title and input_id:
            out.append({"title": title, "id": input_id})
    return out


def pick_exact_input_match(text: str, catalog: list[dict[str, str]]) -> dict[str, str] | None:
    haystack = text.lower()
    for item in catalog:
        title = item["title"].strip()
        if not title:
            continue
        if re.search(rf"(?<![\w-]){re.escape(title.lower())}(?![\w-])", haystack):
            return item
    return None


def find_input_by_title(catalog: list[dict[str, str]], title: str) -> dict[str, str] | None:
    """Ищет input по точному совпадению title (case-insensitive)."""
    t = (title or "").strip()
    if not t:
        return None
    tl = t.lower()
    for item in catalog:
        if (item.get("title") or "").strip().lower() == tl:
            return item
    return None


def enforce_exact_input_filter(query: str, input_id: str) -> str:
    enforced = f"gl2_source_input:{input_id}"
    q = (query or "").strip()
    if not q or q == "*":
        return enforced
    if "gl2_source_input:" in q:
        # Поддерживаем плейсхолдеры вида <id> и shell-подстановки вида $(VAR)
        # без оставления "висящей" закрывающей скобки в запросе.
        return re.sub(
            r"gl2_source_input\s*:\s*(\$\([^)]+\)|<[^>]+>|[^\s)]+)",
            enforced,
            q,
        )
    return f"{enforced} AND ({q})"


def force_exact_input_on_args(name: str | None, args: dict | None, exact_input: dict[str, str] | None) -> dict | None:
    if not name or not args or not exact_input:
        return args
    if name not in {"search_messages", "aggregate_messages"}:
        return args
    forced = dict(args)
    forced["query"] = enforce_exact_input_filter(str(forced.get("query", "*")), exact_input["id"])
    return forced
