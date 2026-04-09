"""Graylog MCP server: search, terms aggregation, streams, inputs."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from typing import Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP


mcp = FastMCP("graylog")

_MAX_SEARCH = 150
_DEFAULT_RANGE = 300
_DEFAULT_MESSAGE_FIELDS = "timestamp,level,source,message,facility,logger,gl2_source_input"
_COUNT_ONLY_FIELDS = "timestamp"
_FALLBACK_PAGE_SIZE = 500
_FALLBACK_MAX_ROWS = 50_000


def _compact_json(data: Any, limit: int | None = None) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if limit and len(text) > limit:
        return text[: limit - 40] + "\n… [обрезано]"
    return text


def _graylog_api_base() -> str:
    explicit = (os.getenv("GRAYLOG_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    legacy = (os.getenv("GRAYLOG_MCP_URL") or "").strip()
    if legacy:
        u = legacy.rstrip("/")
        if u.endswith("/mcp"):
            u = u[:-4]
        return u
    return "http://127.0.0.1:9000/api"


def _graylog_auth() -> httpx.Auth | None:
    token = (os.getenv("GRAYLOG_TOKEN") or os.getenv("GRAYLOG_MCP_AUTH") or "").strip() or None
    user = (os.getenv("GRAYLOG_USER") or "").strip() or None
    password = (os.getenv("GRAYLOG_PASSWORD") or "").strip() or None
    if token:
        return httpx.BasicAuth(token, "token")
    if user and password:
        return httpx.BasicAuth(user, password)
    return None


def _graylog_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "X-Requested-By": (os.getenv("GRAYLOG_X_REQUESTED_BY") or "ai-house").strip() or "ai-house",
    }


def _graylog_config_error() -> str | None:
    if _graylog_auth() is None:
        return (
            "Graylog не настроен: укажите GRAYLOG_TOKEN или пару GRAYLOG_USER + GRAYLOG_PASSWORD "
            "(MCP-сервер читает Graylog_* env напрямую)."
        )
    return None


def _parse_timeframe_to_seconds(raw: str) -> int | None:
    s = (raw or "").strip().lower()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    m = re.match(r"^(\d+)\s*([smhdw])$", s)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}.get(unit, 60)
    return n * mult


def _resolve_range_seconds(range_seconds: int | None, timeframe: str | None, default: int = _DEFAULT_RANGE) -> int:
    if range_seconds is not None:
        try:
            return max(1, int(range_seconds))
        except (TypeError, ValueError):
            pass
    if timeframe is not None:
        parsed = _parse_timeframe_to_seconds(str(timeframe).strip())
        if parsed is not None:
            return max(1, parsed)
    return default


def _format_graylog_http_error(exc: httpx.HTTPStatusError) -> str:
    code = exc.response.status_code
    body = (exc.response.text or "")[:2000]
    if code == 403:
        return (
            "HTTP 403 Forbidden (Graylog): у этой учётной записи или токена нет права выполнять поиск по API.\n"
            "Проверьте роли пользователя и доступ на чтение сообщений.\n"
            f"Тело ответа Graylog: {body}"
        )
    if code == 401:
        return (
            "HTTP 401 Unauthorized: неверный токен или логин/пароль, либо истёк срок токена.\n"
            f"Ответ: {body}"
        )
    return f"HTTP {code}: {body}"


async def _graylog_get(path: str, params: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(
        base_url=_graylog_api_base(),
        auth=_graylog_auth(),
        timeout=httpx.Timeout(60.0),
        verify=(os.getenv("GRAYLOG_VERIFY_SSL") or "true").strip().lower() in {"1", "true", "yes", "y", "on"},
        headers=_graylog_headers(),
    ) as client:
        r = await client.get(path, params=params)
        r.raise_for_status()
        return r.json()


async def _graylog_post(path: str, payload: dict[str, Any]) -> Any:
    async with httpx.AsyncClient(
        base_url=_graylog_api_base(),
        auth=_graylog_auth(),
        timeout=httpx.Timeout(120.0),
        verify=(os.getenv("GRAYLOG_VERIFY_SSL") or "true").strip().lower() in {"1", "true", "yes", "y", "on"},
        headers=_graylog_headers(),
    ) as client:
        r = await client.post(path, json=payload)
        r.raise_for_status()
        return r.json()


def _relative_timerange(range_sec: int) -> dict[str, Any]:
    return {"type": "relative", "range": range_sec}


def _normalize_error_level_query(query: str) -> str:
    q = (query or "").strip()
    if not q:
        return q
    if "level:3" in q.lower():
        return q
    return re.sub(r"(?i)\blevel\s*:\s*error\b", "(level:3 OR level:ERROR)", q)


def _extract_aggregate_buckets(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    schema = data.get("schema") or []
    rows = data.get("datarows") or []
    if not isinstance(schema, list) or not isinstance(rows, list):
        return []

    group_idx: int | None = None
    count_idx: int | None = None
    for idx, column in enumerate(schema):
        if not isinstance(column, dict):
            continue
        if group_idx is None and column.get("column_type") == "grouping":
            group_idx = idx
        if count_idx is None and column.get("column_type") == "metric" and column.get("function") == "count":
            count_idx = idx

    if group_idx is None or count_idx is None:
        return []

    buckets: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        if len(row) <= max(group_idx, count_idx):
            continue
        buckets.append({"value": row[group_idx], "count": row[count_idx]})
    return buckets


def _can_fallback_to_client_side_terms(exc: httpx.HTTPStatusError, field: str) -> bool:
    body = (exc.response.text or "").lower()
    return (
        exc.response.status_code == 400
        and field.strip().lower() in {"message", "full_message", "message.keyword", "full_message.keyword"}
        and "all shards failed" in body
    )


async def _client_side_terms_aggregation(query: str, field: str, range_sec: int, size: int) -> dict[str, Any]:
    requested_field = field.strip()
    field_name = requested_field.removesuffix(".keyword")
    counter: Counter[str] = Counter()
    scanned_rows = 0
    offset = 0
    truncated = False

    while True:
        page_size = min(_FALLBACK_PAGE_SIZE, _FALLBACK_MAX_ROWS - scanned_rows)
        if page_size <= 0:
            truncated = True
            break

        data = await _graylog_post(
            "search/messages",
            {
                "query": query,
                "fields": [field_name],
                "from": offset,
                "size": page_size,
                "timerange": _relative_timerange(range_sec),
            },
        )

        schema = data.get("schema") or []
        rows = data.get("datarows") or []
        if not isinstance(schema, list) or not isinstance(rows, list):
            break

        field_idx: int | None = None
        for idx, column in enumerate(schema):
            if isinstance(column, dict) and column.get("field") == field_name:
                field_idx = idx
                break

        if field_idx is None:
            break

        for row in rows:
            if not isinstance(row, list) or len(row) <= field_idx:
                continue
            value = row[field_idx]
            if value is None:
                continue
            text = str(value).strip()
            if text:
                counter[text] += 1

        fetched = len(rows)
        scanned_rows += fetched
        offset += fetched
        if fetched < page_size:
            break

    buckets = [{"value": value, "count": count} for value, count in counter.most_common(size)]
    return {
        "field": requested_field,
        "field_used": field_name,
        "query": query,
        "range_seconds": range_sec,
        "mode": "client_side_terms_fallback",
        "scanned_rows": scanned_rows,
        "truncated": truncated,
        "max_rows": _FALLBACK_MAX_ROWS,
        "buckets": buckets,
    }


@mcp.tool()
async def search_messages(
    query: str,
    response_shape: Literal["count", "samples"] = "samples",
    timeframe: str | None = None,
    range_seconds: int | None = None,
    limit: int = 25,
    fields: str | None = None,
) -> str:
    """
    Ищет конкретные сообщения по Lucene-запросу.

    Когда использовать:
    - нужны примеры строк логов;
    - нужен простой поиск по условию;
    - нужен только count совпадений через response_shape=count.

    Когда не использовать:
    - если нужен top N самых частых ошибок по всему окну времени;
    - если нужно посчитать частоты значений одного поля.

    Возвращает:
    - response_shape=count: total_results и краткий summary;
    - response_shape=samples: messages + total_results.

    Пример:
    - query="gl2_source_input:<id> AND (level:3 OR level:ERROR)", response_shape="samples", timeframe="1d".
    """
    query = _normalize_error_level_query((query or "").strip())
    if not query:
        return "Укажите query (Lucene-запрос)."
    if (msg := _graylog_config_error()) is not None:
        return msg

    range_sec = _resolve_range_seconds(range_seconds, timeframe)
    count_only = response_shape in ("count", "cardinality")
    fields_arg = (fields or "").strip()

    if count_only:
        limit = 1
        fields_param = _COUNT_ONLY_FIELDS
    else:
        limit = min(max(1, int(limit)), _MAX_SEARCH)
        if fields_arg.lower() == "full":
            fields_param = None
        elif fields_arg:
            fields_param = fields_arg
        else:
            fields_param = _DEFAULT_MESSAGE_FIELDS

    params: dict[str, Any] = {"query": query, "range": range_sec, "limit": limit}
    if fields_param:
        params["fields"] = fields_param

    try:
        data = await _graylog_get("search/universal/relative", params=params)
    except httpx.HTTPStatusError as exc:
        return _format_graylog_http_error(exc)
    except httpx.RequestError as exc:
        return f"Сеть / запрос: {exc!s}"

    messages = data.get("messages") or []
    total = data.get("total_results")

    if count_only:
        return _compact_json(
            {
                "response_shape": "count",
                "total_results": total,
                "range_seconds": range_sec,
                "query": query,
                "note": "Тела сообщений не запрашивались; total_results — полный счётчик по запросу.",
            }
        )

    out: list[dict[str, Any]] = []
    for m in messages:
        msg = m.get("message") if isinstance(m, dict) else None
        if isinstance(msg, dict):
            out.append(msg)
        elif isinstance(m, dict):
            out.append(m)

    return _compact_json(
        {
            "response_shape": "samples",
            "total_results": total,
            "returned": len(out),
            "range_seconds": range_sec,
            "query": query,
            "fields": fields_param or "full",
            "messages": out,
        },
        limit=32_000,
    )


@mcp.tool()
async def aggregate_messages(
    field: str,
    query: str = "*",
    timeframe: str | None = None,
    range_seconds: int | None = None,
    size: int = 20,
) -> str:
    """
    Считает частоты значений одного поля за всё окно времени и возвращает top N бакетов.

    Когда использовать:
    - нужны самые частые ошибки;
    - нужен top 3 / top 10 / рейтинг значений;
    - нужно сгруппировать по message, logger, source, level и т.п.

    Когда не использовать:
    - если нужны примеры строк;
    - если нужен просто count всех совпадений;
    - если нужен поиск по произвольному Lucene-запросу без агрегации.

    Возвращает:
    - JSON с полем, query, range_seconds и response из terms aggregation.

    Пример:
    - field="message", query="gl2_source_input:<id> AND (level:3 OR level:ERROR)", timeframe="1d", size=3.
    """
    field = (field or "").strip()
    if not field:
        return "Укажите field (имя поля)."
    if (msg := _graylog_config_error()) is not None:
        return msg

    query = _normalize_error_level_query((query or "*").strip() or "*")
    range_sec = _resolve_range_seconds(range_seconds, timeframe)
    size = min(max(1, int(size)), 100)

    try:
        data = await _graylog_post(
            "search/aggregate",
            {
                "query": query,
                "timerange": _relative_timerange(range_sec),
                "group_by": [{"field": field, "limit": size}],
                "metrics": [{"function": "count"}],
            },
        )
    except httpx.HTTPStatusError as exc:
        if _can_fallback_to_client_side_terms(exc, field):
            try:
                fallback = await _client_side_terms_aggregation(query, field, range_sec, size)
            except httpx.HTTPStatusError as inner_exc:
                return _format_graylog_http_error(inner_exc)
            except httpx.RequestError as inner_exc:
                return f"Сеть / запрос: {inner_exc!s}"
            return _compact_json(fallback, limit=80_000)
        return _format_graylog_http_error(exc)
    except httpx.RequestError as exc:
        return f"Сеть / запрос: {exc!s}"

    buckets = _extract_aggregate_buckets(data)
    return _compact_json(
        {
            "field": field,
            "query": query,
            "range_seconds": range_sec,
            "mode": "server_aggregate",
            "buckets": buckets,
            "response": data,
        },
        limit=80_000,
    )


@mcp.tool()
async def list_streams() -> str:
    """
    Возвращает список streams.

    Когда использовать:
    - пользователь явно говорит про stream;
    - нужно посмотреть id/title stream.

    Когда не использовать:
    - если пользователь называет окружение, хост или input;
    - если нужно найти gl2_source_input для inhouse1.

    Пример:
    - "покажи streams" или "какой stream у этой системы?".
    """
    if (msg := _graylog_config_error()) is not None:
        return msg
    try:
        data = await _graylog_get("streams")
    except httpx.HTTPStatusError as exc:
        return _format_graylog_http_error(exc)
    except httpx.RequestError as exc:
        return f"Сеть / запрос: {exc!s}"

    streams = data.get("streams") if isinstance(data, dict) else data
    if not isinstance(streams, list):
        return _compact_json(data, limit=80_000)

    slim = []
    for s in streams[:200]:
        if not isinstance(s, dict):
            continue
        slim.append(
            {
                "id": s.get("id"),
                "title": s.get("title"),
                "description": s.get("description"),
                "is_default": s.get("is_default"),
                "disabled": s.get("disabled"),
            }
        )
    return _compact_json({"streams": slim, "count": len(slim)}, limit=80_000)


@mcp.tool()
async def list_inputs() -> str:
    """
    Возвращает список inputs.

    Когда использовать:
    - пользователь называет окружение или input-name вроде inhouse1;
    - нужно сопоставить title input с gl2_source_input:<id>;
    - нужно подготовить фильтр для поиска логов.

    Когда не использовать:
    - если нужен stream;
    - если задача не привязана к конкретному окружению/input.

    Пример:
    - "найди inhouse1 и покажи его id для фильтрации логов".
    """
    if (msg := _graylog_config_error()) is not None:
        return msg
    try:
        data = await _graylog_get("system/inputs")
    except httpx.HTTPStatusError as exc:
        return _format_graylog_http_error(exc)
    except httpx.RequestError as exc:
        return f"Сеть / запрос: {exc!s}"

    raw = data.get("inputs") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return _compact_json(data, limit=80_000)

    slim = []
    for inp in raw[:200]:
        if not isinstance(inp, dict):
            continue
        slim.append(
            {
                "id": inp.get("id"),
                "title": inp.get("title"),
                "type": inp.get("type"),
                "global": inp.get("global"),
            }
        )
    return _compact_json(
        {
            "inputs": slim,
            "count": len(slim),
            "hint": "В поиске: gl2_source_input:<id> AND (level:3 OR level:ERROR).",
        },
        limit=80_000,
    )


if __name__ == "__main__":
    mcp.run()
