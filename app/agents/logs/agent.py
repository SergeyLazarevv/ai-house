"""Агент логов: Graylog через MCP. Цикл: LLM → TOOL_CALL → MCP → снова LLM или финал."""

from __future__ import annotations

import logging
import re
from contextlib import AsyncExitStack

import httpx

from app.agents.base import BaseAgent
from app.agents.logs.inputs import (
    extract_inputs_catalog,
    find_input_by_title,
    force_exact_input_on_args,
    pick_exact_input_match,
)
from app.agents.logs.parse import tool_call_uses_default_time
from app.agents.logs.responses import (
    DEFAULT_TIME_RANGE_SECONDS,
    clip_tool_result_for_llm,
    extract_range_seconds,
    humanize_range_seconds,
    is_tool_error,
    search_messages_empty_likely_placeholder_args,
    with_default_time_note,
)
from app.agents.logs.system_prompt import build_logs_system_prompt
from app.config import AppConfig
from app.shared.connectors.graylog import GraylogConnector
from app.shared.llm import build_llm
from app.shared.tool_parser import parse_all_tool_calls, parse_tool_call

_LOG = logging.getLogger(__name__)


def _query_has_placeholder(args: dict | None) -> bool:
    if not isinstance(args, dict):
        return False
    query = args.get("query")
    if not isinstance(query, str):
        return False
    return bool(re.search(r"<[^>]+>", query))


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
            enforced_title = (self._config.graylog.input_title or "").strip() or None
            user_text = f"{context}\n\nЗапрос: {message}" if context else message
            system = build_logs_system_prompt(self._connector.tools)
            tool_names = self._connector.tool_names
            default_time_seconds: int | None = None
            exact_input: dict[str, str] | None = None
            if enforced_title:
                raw = await self._connector.call_tool("list_inputs", {})
                if is_tool_error(raw):
                    return raw.strip()
                catalog = extract_inputs_catalog(raw)
                exact_input = find_input_by_title(catalog, enforced_title)
                if not exact_input:
                    available = ", ".join(f"`{i['title']}`" for i in catalog[:50]) or "(пусто)"
                    return (
                        "Агент логов: не найден Graylog input с заданным title из `GRAYLOG_INPUT_TITLE`.\n\n"
                        f"- Искомый title: `{enforced_title}`\n"
                        f"- Доступные inputs: {available}"
                    ).strip()
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
                reply_text = (reply or "").strip()
                if reply_text == "ALTERNATIVE_STATUS_TOOL_CALLS":
                    return (
                        "Агент логов: сервис LLM вернул служебный маркер вместо ответа. "
                        "Запрос к Graylog не выполнен."
                    )
                batch = parse_all_tool_calls(reply_text, tool_names)
                if not batch:
                    name, args = parse_tool_call(reply_text, tool_names)
                    if name and args is not None:
                        batch = [(name, args)]
                if not batch:
                    if "tool_call" in reply_text.lower():
                        return (
                            "Агент логов: сервис LLM вернул некорректный формат TOOL_CALL. "
                            "Запрос к Graylog не выполнен."
                        )
                    return (
                        with_default_time_note(reply.strip(), default_time_seconds)
                        if default_time_seconds
                        else reply.strip()
                    )
                result_blocks: list[str] = []
                placeholder_search_hint = ""
                for idx, (name, args) in enumerate(batch, start=1):
                    used_default_time = bool(name and tool_call_uses_default_time(name, args))
                    args = force_exact_input_on_args(name, args, exact_input)
                    if name in {"search_messages", "aggregate_messages"} and _query_has_placeholder(args):
                        return (
                            "Агент логов: в query обнаружен плейсхолдер вида `<...>` "
                            "(например `<GRAYLOG_INPUT_TITLE>`), Graylog не подставляет такие значения автоматически. "
                            "Укажите реальное значение фильтра или задайте `GRAYLOG_INPUT_TITLE`, чтобы агент мог "
                            "принудительно проставить корректный `gl2_source_input:<id>`."
                        )
                    result = await self._connector.call_tool(name, args or {})
                    if used_default_time:
                        default_time_seconds = (
                            extract_range_seconds(result)
                            or default_time_seconds
                            or DEFAULT_TIME_RANGE_SECONDS
                        )
                    if is_tool_error(result):
                        _LOG.info("logs agent: tool returned error, returning to user")
                        return result.strip()
                    if name == "list_inputs":
                        catalog = extract_inputs_catalog(result)
                        if not enforced_title:
                            exact_input = pick_exact_input_match(f"{context}\n{message}", catalog)
                    clipped = clip_tool_result_for_llm(result)
                    if len(clipped) < len(result):
                        _LOG.info(
                            "logs agent: tool result clipped %s -> %s chars",
                            len(result),
                            len(clipped),
                        )
                    label = f"[Результат {name}]" if len(batch) == 1 else f"[Результат {name} #{idx}]"
                    result_blocks.append(f"{label}:\n{clipped}")
                    if name == "search_messages" and search_messages_empty_likely_placeholder_args(
                        result.strip()
                    ):
                        placeholder_search_hint = (
                            "Инвариант среды: `search_messages` вернул 0 записей, а в сохранённом `query` "
                            "есть шаблон в угловых скобках (`<...>`) — Graylog воспринимает это как буквальную "
                            "строку, не как подстановку. Нельзя завершать ответ только счётчиками из агрегации, "
                            "если пользователь просил детали/примеры сообщений: сделай новые вызовы "
                            "`search_messages` с **реальными** значениями из `buckets[].value` последнего "
                            "`aggregate_messages` (например `source:\"notification-service\"`). "
                            "Не помещай в один ответ `aggregate_messages` и зависящий от него `search_messages` "
                            "с плейсхолдерами — сначала только агрегация, затем отдельным сообщением поиски "
                            "с литералами. Если после исправления выборка пуста — так и напиши пользователю.\n\n"
                        )
                closing = (
                    placeholder_search_hint
                    + (
                        f"Важно: для окружения используй exact-match по title: `{exact_input['title']}` "
                        f"с `gl2_source_input:{exact_input['id']}`. Не подменяй его похожими input вроде суффиксов `-nginx` или `-apigw`.\n\n"
                        if exact_input
                        else ""
                    )
                    + (
                        "Важно: пользователь не указал время, поэтому для выборки было использовано окно "
                        f"по умолчанию: {humanize_range_seconds(default_time_seconds)}. "
                        "Обязательно явно укажи это в ответе пользователю.\n\n"
                        if default_time_seconds
                        else ""
                    )
                    + (
                        "Сначала выполни исправленные вызовы инструментов по подсказке выше; итог пользователю "
                        "— когда есть примеры `message` из JSON или явно указано, что примеров нет.\n\n"
                        if placeholder_search_hint
                        else ""
                    )
                    + (
                        "Дай итоговый ответ пользователю."
                        if not placeholder_search_hint
                        else "После успешных вызовов дай итоговый ответ пользователю (не отсылай к «дополнительному поиску» снаружи агента)."
                    )
                )
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": "\n\n".join(result_blocks) + "\n\n" + closing})
        return "Агент логов: лимит итераций."
