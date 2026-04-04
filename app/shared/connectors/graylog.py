"""Graylog REST API: поиск, агрегация terms, список стримов."""

from __future__ import annotations

import json
import re
from contextlib import AsyncExitStack
from typing import Any

import httpx

from app.config import GraylogConfig

from .base import BaseConnector

_MAX_SEARCH = 150
_DEFAULT_RANGE = 300
# Узкий набор полей по умолчанию: Graylog отдаёт только их, без мегабайтных служебных/сырых полей.
_DEFAULT_MESSAGE_FIELDS = "timestamp,level,source,message,facility,logger,gl2_source_input"
# Минимальный запрос к API при подсчёте: limit=1 и одно поле — total_results всё равно полный.
_COUNT_ONLY_FIELDS = "timestamp"


def _compact_json(data: Any, limit: int | None = None) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if limit and len(text) > limit:
        return text[: limit - 40] + "\n… [обрезано]"
    return text


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


def _format_graylog_http_error(exc: httpx.HTTPStatusError) -> str:
    code = exc.response.status_code
    body = (exc.response.text or "")[:2000]
    if code == 403:
        return (
            "HTTP 403 Forbidden (Graylog): у этой учётной записи или токена нет права выполнять поиск по API.\n"
            "В веб-интерфейсе Graylog: System → Users → ваш пользователь / service account → роли: "
            "нужна роль с правом поиска сообщений (часто достаточно встроенной Reader; "
            "если роль кастомная — добавьте разрешения категории Messages / Search).\n"
            "Проверьте, что Personal Access Token создан у того же пользователя, которому назначена роль.\n"
            f"Тело ответа Graylog: {body}"
        )
    if code == 401:
        return (
            "HTTP 401 Unauthorized: неверный токен или логин/пароль, либо истёк срок токена.\n"
            f"Ответ: {body}"
        )
    return f"HTTP {code}: {body}"


def _resolve_range_seconds(args: dict, default: int = _DEFAULT_RANGE) -> int:
    """range_seconds или timeframe (300, 5m, 10m, 1h)."""
    rs = args.get("range_seconds")
    if rs is not None:
        try:
            return max(1, int(rs))
        except (TypeError, ValueError):
            pass
    tf = args.get("timeframe")
    if tf is not None:
        parsed = _parse_timeframe_to_seconds(str(tf).strip())
        if parsed is not None:
            return max(1, parsed)
    return default


class GraylogConnector(BaseConnector):
    def __init__(self, config: GraylogConfig) -> None:
        self._cfg = config
        self._client: httpx.AsyncClient | None = None
        self._tools: list[dict] = []

    @property
    def is_configured(self) -> bool:
        return self._cfg.is_configured

    def _base(self) -> str:
        return self._cfg.api_base.rstrip("/") + "/"

    def _auth(self) -> httpx.Auth | None:
        if self._cfg.token:
            return httpx.BasicAuth(self._cfg.token, "token")
        if self._cfg.username and self._cfg.password:
            return httpx.BasicAuth(self._cfg.username, self._cfg.password)
        return None

    async def connect(self, stack: AsyncExitStack) -> None:
        self._client = httpx.AsyncClient(
            base_url=self._base(),
            auth=self._auth(),
            timeout=httpx.Timeout(60.0),
            verify=self._cfg.verify_ssl,
            headers={
                "Accept": "application/json",
                # Graylog ожидает заголовок для API-клиентов (CSRF / совместимость с прокси).
                "X-Requested-By": self._cfg.x_requested_by,
            },
        )
        await stack.enter_async_context(self._client)

        self._tools = [
            {
                "name": "search_messages",
                "description": (
                    "Поиск логов (Lucene). Окно: range_seconds или timeframe. "
                    "response_shape: count — только total_results (без тел сообщений в ответе инструмента); "
                    "samples — выборка строк; по умолчанию Graylog получает узкий fields (см. ниже), полный набор только если fields=full. "
                    "Input: gl2_source_input:ID; имя input — через list_inputs."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Lucene-запрос"},
                        "response_shape": {
                            "type": "string",
                            "description": "count — вопросы «сколько» (только total_results); samples — посмотреть примеры строк",
                            "enum": ["count", "samples"],
                        },
                        "timeframe": {
                            "type": "string",
                            "description": 'Окно назад: "5m", "1h", "7d", "1w" или секунды строкой',
                        },
                        "range_seconds": {
                            "type": "integer",
                            "description": f"Интервал от «сейчас» назад, сек (по умолчанию {_DEFAULT_RANGE})",
                        },
                        "limit": {
                            "type": "integer",
                            "description": f"Только при response_shape=samples: сколько сообщений (по умолчанию 25, макс. {_MAX_SEARCH})",
                        },
                        "fields": {
                            "type": "string",
                            "description": "Поля через запятую. Пропуск или пусто — узкий безопасный набор для LLM; full — все поля Graylog (тяжёлый запрос)",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "aggregate_messages",
                "description": (
                    "Топ значений поля (terms). Окно: range_seconds или timeframe (5m). "
                    "Для распределения уровней: field=level."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string", "description": "Имя поля для группировки"},
                        "query": {"type": "string", "description": "Lucene-фильтр (по умолчанию *)"},
                        "timeframe": {"type": "string", "description": "Как в search_messages: 5m, 1h, 7d, 1w"},
                        "range_seconds": {"type": "integer", "description": "Интервал назад, сек"},
                        "size": {"type": "integer", "description": "Сколько топ-значений (по умолчанию 20)"},
                    },
                    "required": ["field"],
                },
            },
            {
                "name": "list_streams",
                "description": "Список стримов Graylog (id, title). Без аргументов.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "list_inputs",
                "description": (
                    "Список Inputs (приёмников логов): id и title. Нужен для фильтра gl2_source_input:ID в поиске; "
                    "название input ≠ stream."
                ),
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]

    @property
    def tools(self) -> list[dict]:
        return self._tools

    async def call_tool(self, name: str, args: dict) -> str:
        if not self._client:
            return "Ошибка: коннектор не подключён (connect не вызван)."
        try:
            if name == "search_messages":
                return await self._search_messages(args)
            if name == "aggregate_messages":
                return await self._aggregate_terms(args)
            if name == "list_streams":
                return await self._list_streams()
            if name == "list_inputs":
                return await self._list_inputs()
        except httpx.HTTPStatusError as e:
            return _format_graylog_http_error(e)
        except httpx.RequestError as e:
            return f"Сеть / запрос: {e!s}"
        return f"Неизвестный инструмент: {name}"

    def _int(self, args: dict, key: str, default: int) -> int:
        v = args.get(key)
        if v is None:
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    async def _search_messages(self, args: dict) -> str:
        query = (args.get("query") or "").strip()
        if not query:
            return "Укажите query (Lucene-запрос)."
        range_sec = _resolve_range_seconds(args, _DEFAULT_RANGE)
        shape_raw = (args.get("response_shape") or "samples").strip().lower()
        count_only = shape_raw in ("count", "cardinality")

        fields_arg = (args.get("fields") or "").strip()
        if count_only:
            limit = 1
            fields_param = _COUNT_ONLY_FIELDS
        else:
            limit = min(max(1, self._int(args, "limit", 25)), _MAX_SEARCH)
            if fields_arg.lower() == "full":
                fields_param = None
            elif fields_arg:
                fields_param = fields_arg
            else:
                fields_param = _DEFAULT_MESSAGE_FIELDS

        params: dict[str, Any] = {
            "query": query,
            "range": range_sec,
            "limit": limit,
        }
        if fields_param:
            params["fields"] = fields_param

        r = await self._client.get("search/universal/relative", params=params)
        r.raise_for_status()
        data = r.json()
        messages = data.get("messages") or []
        total = data.get("total_results")

        if count_only:
            summary = {
                "response_shape": "count",
                "total_results": total,
                "range_seconds": range_sec,
                "query": query,
                "note": "Тела сообщений не запрашивались; total_results — полный счётчик по запросу. Для примеров вызови снова с response_shape=samples.",
            }
            return _compact_json(summary)

        out: list[dict[str, Any]] = []
        for m in messages:
            msg = m.get("message") if isinstance(m, dict) else None
            if isinstance(msg, dict):
                out.append(msg)
            elif isinstance(m, dict):
                out.append(m)

        summary = {
            "response_shape": "samples",
            "total_results": total,
            "returned": len(out),
            "range_seconds": range_sec,
            "query": query,
            "fields": fields_param or "full",
            "messages": out,
        }
        return _compact_json(summary, limit=80_000)

    async def _aggregate_terms(self, args: dict) -> str:
        field = (args.get("field") or "").strip()
        if not field:
            return "Укажите field (имя поля)."
        query = (args.get("query") or "*").strip() or "*"
        range_sec = _resolve_range_seconds(args, _DEFAULT_RANGE)
        size = min(max(1, self._int(args, "size", 20)), 100)

        params = {
            "field": field,
            "query": query,
            "range": range_sec,
            "size": size,
        }
        r = await self._client.get("search/universal/relative/terms", params=params)
        r.raise_for_status()
        data = r.json()
        return _compact_json(
            {
                "field": field,
                "query": query,
                "range_seconds": range_sec,
                "response": data,
            },
            limit=80_000,
        )

    async def _list_streams(self) -> str:
        r = await self._client.get("streams")
        r.raise_for_status()
        data = r.json()
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

    async def _list_inputs(self) -> str:
        r = await self._client.get("system/inputs")
        r.raise_for_status()
        data = r.json()
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
                "hint": "В поиске: gl2_source_input:<id> AND (level:3 OR level:ERROR). Не использовать stream:имя_input.",
            },
            limit=80_000,
        )
