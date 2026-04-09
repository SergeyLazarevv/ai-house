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


def _fallback_tool_from_json(reply: str, tool_names: list[str]) -> tuple[str | None, dict | None, bool]:
    m = re.search(r"\{[\s\S]*\}", reply.strip())
    if not m:
        return None, None, False
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None, None, False
    if not isinstance(obj, dict):
        return None, None, False

    tf_kw = _timeframe_kwargs_from_obj(obj)
    used_default_time = not tf_kw

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
        return "aggregate_messages", args_out, used_default_time

    if "query" not in obj or "search_messages" not in tool_names:
        return None, None, False

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
    return "search_messages", args_out, used_default_time


_LOG = logging.getLogger(__name__)
_MAX_TOOL_RESULT_CHARS = 24_000
_DEFAULT_TIME_RANGE_SECONDS = 300


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


def _tool_call_uses_default_time(name: str, args: dict | None) -> bool:
    if name not in {"search_messages", "aggregate_messages"}:
        return False
    payload = args or {}
    return "range_seconds" not in payload and "timeframe" not in payload


def _extract_range_seconds(text: str) -> int | None:
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


def _humanize_range_seconds(seconds: int) -> str:
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


def _with_default_time_note(text: str, seconds: int | None) -> str:
    if not text:
        return text
    used_seconds = seconds or _DEFAULT_TIME_RANGE_SECONDS
    note = (
        f"- Время в запросе не было указано, поэтому использовано окно по умолчанию: "
        f"{_humanize_range_seconds(used_seconds)}."
    )
    marker = "2. Факты:\n"
    if marker in text and note not in text:
        return text.replace(marker, f"{marker}{note}\n", 1)
    if note in text:
        return text
    suffix = (
        "\n\nПримечание: время в запросе не было указано, "
        f"поэтому использовано окно по умолчанию: {_humanize_range_seconds(used_seconds)}."
    )
    return text.rstrip() + suffix


def _extract_inputs_catalog(text: str) -> list[dict[str, str]]:
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


def _pick_exact_input_match(text: str, catalog: list[dict[str, str]]) -> dict[str, str] | None:
    haystack = text.lower()
    for item in catalog:
        title = item["title"].strip()
        if not title:
            continue
        if re.search(rf"(?<![\w-]){re.escape(title.lower())}(?![\w-])", haystack):
            return item
    return None


def _enforce_exact_input_filter(query: str, input_id: str) -> str:
    enforced = f"gl2_source_input:{input_id}"
    q = (query or "").strip()
    if not q or q == "*":
        return enforced
    if "gl2_source_input:" in q:
        return re.sub(r"gl2_source_input\s*:\s*([^\s)]+)", enforced, q)
    return f"{enforced} AND ({q})"


def _force_exact_input_on_args(name: str | None, args: dict | None, exact_input: dict[str, str] | None) -> dict | None:
    if not name or not args or not exact_input:
        return args
    if name not in {"search_messages", "aggregate_messages"}:
        return args
    forced = dict(args)
    forced["query"] = _enforce_exact_input_filter(str(forced.get("query", "*")), exact_input["id"])
    return forced


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
            default_time_seconds: int | None = None
            exact_input: dict[str, str] | None = None
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
                used_default_time = False
                if name:
                    used_default_time = _tool_call_uses_default_time(name, args)
                if not name:
                    name, args, used_default_time = _fallback_tool_from_json(reply, tool_names)
                if not name:
                    return _with_default_time_note(reply.strip(), default_time_seconds) if default_time_seconds else reply.strip()
                args = _force_exact_input_on_args(name, args, exact_input)
                result = await self._connector.call_tool(name, args or {})
                if used_default_time:
                    default_time_seconds = _extract_range_seconds(result) or default_time_seconds or _DEFAULT_TIME_RANGE_SECONDS
                if _is_tool_error(result):
                    _LOG.info("logs agent: tool returned error, short-circuiting to user")
                    return result.strip()
                if name == "list_inputs":
                    catalog = _extract_inputs_catalog(result)
                    exact_input = _pick_exact_input_match(f"{context}\n{message}", catalog)
                if deterministic := _deterministic_aggregate_answer(name, result, message, context):
                    return _with_default_time_note(deterministic, default_time_seconds) if default_time_seconds else deterministic
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
                            f"[Результат {name}]:\n{clipped}\n\n"
                            + (
                                f"Важно: для окружения используй exact-match по title: `{exact_input['title']}` "
                                f"с `gl2_source_input:{exact_input['id']}`. Не подменяй его похожими input вроде суффиксов `-nginx` или `-apigw`.\n\n"
                                if exact_input
                                else ""
                            )
                            + (
                                "Важно: пользователь не указал время, поэтому для выборки было использовано окно "
                                f"по умолчанию: {_humanize_range_seconds(default_time_seconds)}. "
                                "Обязательно явно укажи это в ответе пользователю.\n\n"
                                if default_time_seconds
                                else ""
                            )
                            + "Дай итоговый ответ пользователю."
                        ),
                    }
                )
        return "Агент логов: лимит итераций."
