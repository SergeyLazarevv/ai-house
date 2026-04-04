"""Агент логов: только Graylog (поиск, агрегация, стримы). Не импортирует других агентов."""

from __future__ import annotations

import json
import re
from contextlib import AsyncExitStack

from app.agents.base import BaseAgent
from app.agents.prompt_loader import load_agent_prompt
from app.config import AppConfig
from app.shared.connectors.graylog import GraylogConnector
from app.shared.llm import build_llm
from app.shared.tool_parser import parse_tool_call

_TOOL_CALL_GUIDE = """
Формат вызова инструмента (обязательно так, без «голого» JSON):

Input (приёмник, GELF UDP и т.д.) ≠ Stream. Имя вроде inhouse1 — это title Input.
Нельзя писать stream:inhouse1. Сначала list_inputs, возьми id, затем в query: gl2_source_input:ID.

TOOL_CALL: list_inputs
{}

Вопросы «сколько» / cardinality — только response_shape=count (без тел сообщений в ответе Graylog):
TOOL_CALL: search_messages
{"query": "gl2_source_input:ВСТАВЬ_ID AND (level:3 OR level:ERROR OR level:error)", "timeframe": "5d", "response_shape": "count"}

Примеры строк (узкие поля по умолчанию на стороне Graylog):
TOOL_CALL: search_messages
{"query": "gl2_source_input:ВСТАВЬ_ID AND (level:3 OR level:ERROR)", "timeframe": "5m", "response_shape": "samples", "limit": 15}

Полные поля сообщения (тяжело для LLM — только если нужно):
TOOL_CALL: search_messages
{"query": "...", "timeframe": "1h", "response_shape": "samples", "fields": "full", "limit": 5}

Для распределения уровней:
TOOL_CALL: aggregate_messages
{"field": "level", "query": "*", "timeframe": "5m", "size": 15}

Список стримов (не путать с inputs):
TOOL_CALL: list_streams
{}

Для «сколько ошибок» обязательно response_shape=count. Для топов — aggregate_messages. Не запрашивай samples с fields=full без необходимости.
"""


def _parse_timeframe_to_seconds(raw: str) -> int | None:
    s = (raw or "").strip().lower()
    if not s:
        return None
    m = re.match(r"^(\d+)\s*([smhdw])$", s)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}.get(unit, 60)
    return n * mult


def _fallback_tool_from_json(reply: str, tool_names: list[str]) -> tuple[str | None, dict | None]:
    """Если модель вернула JSON без TOOL_CALL — маппинг на search_messages."""
    if "search_messages" not in tool_names:
        return None, None
    m = re.search(r"\{[\s\S]*\}", reply.strip())
    if not m:
        return None, None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None, None
    if not isinstance(obj, dict) or "query" not in obj:
        return None, None
    q = str(obj.get("query", "*")).strip() or "*"
    range_sec = 300
    if "range_seconds" in obj and obj["range_seconds"] is not None:
        try:
            range_sec = int(obj["range_seconds"])
        except (TypeError, ValueError):
            pass
    elif "timeframe" in obj and obj["timeframe"] is not None:
        parsed = _parse_timeframe_to_seconds(str(obj["timeframe"]))
        if parsed is not None:
            range_sec = parsed
    limit = 20
    if "limit" in obj and obj["limit"] is not None:
        try:
            limit = int(obj["limit"])
        except (TypeError, ValueError):
            pass
    args_out: dict = {"query": q, "range_seconds": range_sec, "limit": limit}
    rs = obj.get("response_shape")
    if isinstance(rs, str) and rs.strip().lower() in ("count", "samples"):
        args_out["response_shape"] = rs.strip().lower()
    return "search_messages", args_out


class LogsAgent(BaseAgent):
    """Специализированный агент: инструменты Graylog через REST API."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._llm = build_llm(config)
        self._connector = GraylogConnector(config.graylog)

    async def run(self, message: str, context: str = "") -> str:
        if not self._connector.is_configured:
            return (
                "Агент логов: Graylog не настроен. Укажите GRAYLOG_URL и GRAYLOG_TOKEN "
                "(Personal Access Token) или GRAYLOG_USER и GRAYLOG_PASSWORD."
            )
        user_text = f"{context}\n\nЗапрос: {message}" if context else message
        system = load_agent_prompt("logs") + "\n" + _TOOL_CALL_GUIDE
        async with AsyncExitStack() as stack:
            await self._connector.connect(stack)
            tools = self._connector.tools
            tool_names = self._connector.tool_names
            messages = [
                {"role": "system", "content": system + "\nИнструменты: " + ", ".join(tool_names)},
                {"role": "user", "content": user_text},
            ]
            for _ in range(5):
                reply = await self._llm.complete(messages)
                name, args = parse_tool_call(reply, tool_names)
                if not name:
                    name, args = _fallback_tool_from_json(reply, tool_names)
                if not name:
                    return reply.strip()
                result = await self._connector.call_tool(name, args or {})
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"[Результат {name}]:\n{result}\n\nДай итоговый ответ пользователю."})
        return "Агент логов: лимит итераций."
