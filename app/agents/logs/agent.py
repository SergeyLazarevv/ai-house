"""Агент логов: Graylog через MCP. Цикл: LLM → TOOL_CALL → MCP → снова LLM или финал."""

from __future__ import annotations

import json
import logging
import re
from contextlib import AsyncExitStack

import httpx

from app.agents.base import BaseAgent
from app.agents.prompt_loader import load_agent_prompt
from app.config import AppConfig
from app.shared.connectors.graylog import GraylogConnector, format_tools_for_llm
from app.shared.llm import build_llm
from app.shared.tool_parser import parse_tool_call

_TOOL_CALL_FORMAT = """
## Формат вызова
`TOOL_CALL: <имя>` и следующей строкой JSON аргументов по схеме каталога.

Примеры:
TOOL_CALL: list_inputs
{}

TOOL_CALL: <tool_name_from_catalog>
{...}
"""

def _timeframe_kwargs_from_obj(obj: dict) -> dict:
    out: dict = {}
    if "range_seconds" in obj and obj["range_seconds"] is not None:
        try:
            out["range_seconds"] = int(obj["range_seconds"])
        except (TypeError, ValueError):
            pass
    if "timeframe" in obj and obj["timeframe"] is not None:
        out["timeframe"] = str(obj["timeframe"]).strip()
    return out


def _fallback_tool_from_json(reply: str, tool_names: list[str]) -> tuple[str | None, dict | None]:
    m = re.search(r"\{[\s\S]*\}", reply.strip())
    if not m:
        return None, None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None, None
    if not isinstance(obj, dict):
        return None, None

    tf_kw = _timeframe_kwargs_from_obj(obj)

    field = obj.get("field")
    if isinstance(field, str) and field.strip() and "aggregate_messages" in tool_names:
        args_out: dict = {
            "field": field.strip(),
            "query": str(obj.get("query", "*")).strip() or "*",
        }
        args_out.update(tf_kw)
        if "size" in obj and obj["size"] is not None:
            try:
                args_out["size"] = int(obj["size"])
            except (TypeError, ValueError):
                pass
        return "aggregate_messages", args_out

    if "query" not in obj or "search_messages" not in tool_names:
        return None, None

    q = str(obj.get("query", "*")).strip() or "*"
    args_out = {"query": q, **tf_kw}
    if "range_seconds" not in args_out and "timeframe" not in args_out:
        args_out["range_seconds"] = 300
    if "limit" in obj and obj["limit"] is not None:
        try:
            args_out["limit"] = int(obj["limit"])
        except (TypeError, ValueError):
            pass
    rs = obj.get("response_shape")
    if isinstance(rs, str) and rs.strip().lower() in ("count", "samples"):
        args_out["response_shape"] = rs.strip().lower()
    return "search_messages", args_out


_LOG = logging.getLogger(__name__)
_MAX_TOOL_RESULT_CHARS = 24_000


def _extract_buckets(text: str) -> list[dict] | None:
    try:
        data = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    buckets = data.get("buckets")
    if not isinstance(buckets, list):
        return None
    out: list[dict] = []
    for item in buckets:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        count = item.get("count")
        if value is None or count is None:
            continue
        try:
            cnt = int(count)
        except (TypeError, ValueError):
            continue
        out.append({"value": str(value), "count": cnt})
    return out


def _short_bucket_value(value: str, limit: int = 160) -> str:
    text = (value or "").strip().replace("\\\\", "\\")
    if not text:
        return "(пустое значение)"
    first_line = text.splitlines()[0].strip()
    first_line = re.sub(r"\s+in\s+/.*$", "", first_line)
    if len(first_line) > limit:
        return first_line[: limit - 1].rstrip() + "…"
    return first_line


def _context_label(message: str, context: str) -> str:
    combined = f"{context}\n{message}".lower()
    env = re.search(r"\b(inhouse[\w-]*)\b", combined)
    if env:
        return env.group(1)
    return "выбранном окружении"


def _time_label(message: str, context: str) -> str:
    combined = f"{context}\n{message}".lower()
    if "сут" in combined or "1d" in combined:
        return "за последние сутки"
    if "недел" in combined or "7d" in combined:
        return "за последнюю неделю"
    if "час" in combined or "1h" in combined:
        return "за последний час"
    return "за указанный период"


def _deterministic_aggregate_answer(tool_name: str, result: str, message: str, context: str) -> str | None:
    if tool_name != "aggregate_messages":
        return None
    buckets = _extract_buckets(result)
    if buckets is None:
        return None

    scope = f"{_time_label(message, context)} в {_context_label(message, context)}"
    if not buckets:
        return (
            f"1. Кратко: {scope} ошибки по заданному фильтру не найдены.\n"
            "2. Факты:\n"
            "- Инструмент агрегации вернул 0 бакетов.\n"
            "3. Вывод: либо по этому фильтру действительно нет ошибок, либо запрос нужно уточнить."
        )

    facts = "\n".join(
        f"- `{_short_bucket_value(item['value'])}` — {item['count']} раз."
        for item in buckets[:10]
    )
    top_value = _short_bucket_value(buckets[0]["value"])
    top_count = buckets[0]["count"]
    return (
        f"1. Кратко: {scope} зафиксированы {min(3, len(buckets))} наиболее частые ошибки.\n"
        "2. Факты:\n"
        f"{facts}\n"
        f"3. Вывод: наиболее частая ошибка — `{top_value}` ({top_count} раз); остальные пункты отсортированы по числу вхождений."
    )


def _is_tool_error(text: str) -> bool:
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
    return t.startswith(error_markers) or "не удалось" in t


def _clip_tool_result_for_llm(text: str) -> str:
    if len(text) <= _MAX_TOOL_RESULT_CHARS:
        return text
    head = _MAX_TOOL_RESULT_CHARS - 200
    return (
        text[:head]
        + "\n\n… [ответ инструмента обрезан: слишком много данных для LLM. "
        "Используй response_shape=count, aggregate_messages или меньший limit.]\n"
    )


def _build_logs_system_prompt(tools: list[dict]) -> str:
    parts = [
        load_agent_prompt("logs").strip(),
        _TOOL_CALL_FORMAT.strip(),
        format_tools_for_llm(tools).strip(),
    ]
    return "\n\n".join(p for p in parts if p)


class LogsAgent(BaseAgent):
    """Специалист Graylog: чистый MCP-цикл без скрытой маршрутизации."""

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
        async with AsyncExitStack() as stack:
            try:
                await self._connector.connect(stack)
            except Exception as e:
                return f"Агент логов: не удалось поднять Graylog MCP-сервер: {e!s}"
            user_text = f"{context}\n\nЗапрос: {message}" if context else message
            system = _build_logs_system_prompt(self._connector.tools)
            tool_names = self._connector.tool_names
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ]
            for _ in range(5):
                try:
                    reply = await self._llm.complete(messages)
                except httpx.HTTPStatusError as e:
                    body = (e.response.text or "")[:1500] if e.response is not None else ""
                    _LOG.warning(
                        "logs agent LLM HTTP %s: %s",
                        getattr(e.response, "status_code", "?"),
                        body,
                    )
                    return (
                        "Агент логов: провайдер LLM вернул ошибку (часто из‑за слишком большого ответа Graylog). "
                        "Попробуйте response_shape=count, aggregate_messages или меньший limit. "
                        f"HTTP {getattr(e.response, 'status_code', '?')}. {body}"
                    ).strip()
                name, args = parse_tool_call(reply, tool_names)
                if not name:
                    name, args = _fallback_tool_from_json(reply, tool_names)
                if not name:
                    return reply.strip()
                result = await self._connector.call_tool(name, args or {})
                if _is_tool_error(result):
                    _LOG.info("logs agent: tool returned error, short-circuiting to user")
                    return result.strip()
                if deterministic := _deterministic_aggregate_answer(name, result, message, context):
                    return deterministic
                clipped = _clip_tool_result_for_llm(result)
                if len(clipped) < len(result):
                    _LOG.info(
                        "logs agent: tool result clipped %s -> %s chars",
                        len(result),
                        len(clipped),
                    )
                messages.append({"role": "assistant", "content": reply})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"[Результат {name}]:\n{clipped}\n\nДай итоговый ответ пользователю."
                        ),
                    }
                )
        return "Агент логов: лимит итераций."
