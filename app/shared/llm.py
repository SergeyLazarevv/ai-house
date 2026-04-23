"""Провайдеры LLM: Yandex, Anthropic (Claude), OpenAI-совместимый API. Выбор через LLM_PROVIDER в env."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import httpx

from app.config import AppConfig

import json
import logging


_LOG = logging.getLogger("ai_house.llm")
_LOG_MAX_CHARS = 12_000


def _msg_text(m: dict[str, Any]) -> str:
    c = m.get("content", m.get("text"))
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    return str(c)


def _truncate_text(text: str, limit: int = _LOG_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 80] + "\n… [обрезано]"


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except TypeError:
        return str(value)


def _logged_messages(messages: list[dict[str, Any]]) -> str:
    compact: list[dict[str, Any]] = []
    for m in messages:
        compact.append(
            {
                "role": m.get("role"),
                "content": _truncate_text(_msg_text(m), limit=2500),
            }
        )
    return _truncate_text(_safe_json(compact))


def _extract_text_from_value(value: Any) -> str:
    """Достаёт текст из ответа провайдера, даже если тот вернул вложенные блоки."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            text = _extract_text_from_value(item)
            if text:
                parts.append(text)
        return "\n".join(parts)
    if isinstance(value, dict):
        for key in ("text", "content", "message", "output"):
            if key in value:
                text = _extract_text_from_value(value.get(key))
                if text:
                    return text
        # Частые блоки у OpenAI/Anthropic-подобных ответов.
        for key in ("choices", "alternatives", "content"):
            if key in value:
                text = _extract_text_from_value(value.get(key))
                if text:
                    return text
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("message"), dict):
            text = _extract_text_from_value(value["message"])
            if text:
                return text
        # Последний безопасный fallback: сериализуем содержимое без вложенных объектов.
        flat = []
        for k, v in value.items():
            if k in {"role", "type"}:
                continue
            if isinstance(v, (str, int, float, bool)) or v is None:
                flat.append(str(v) if v is not None else "")
        return "\n".join(part for part in flat if part)
    return str(value)


def _extract_yandex_text(data: dict[str, Any]) -> str:
    result = data.get("result") or {}
    alternatives = result.get("alternatives") or []
    for alt in alternatives:
        if not isinstance(alt, dict):
            continue
        message = alt.get("message")
        if message is None:
            continue
        if isinstance(message, dict):
            tool_list = ((message.get("toolCallList") or {}).get("toolCalls") or [])
            if isinstance(tool_list, list) and tool_list:
                first = tool_list[0] if isinstance(tool_list[0], dict) else {}
                fn = first.get("functionCall") if isinstance(first, dict) else {}
                if isinstance(fn, dict):
                    raw_name = str(fn.get("name") or "").strip()
                    args = fn.get("arguments") or {}
                    name = raw_name
                    if raw_name.lower().startswith("tool_call:"):
                        name = raw_name.split(":", 1)[1].strip()
                    if name:
                        if not isinstance(args, dict):
                            args = {}
                        return f"TOOL_CALL: {name}\n{json.dumps(args, ensure_ascii=False)}"
        text = _extract_text_from_value(message)
        if text:
            return text
    return ""


@runtime_checkable
class LLM(Protocol):
    async def complete(self, messages: list[dict[str, Any]]) -> str: ...


class YandexLLM:
    def __init__(self, api_key: str, catalog_id: str, model: str = "yandexgpt-lite"):
        self._api_key = api_key
        self._catalog_id = catalog_id
        self._model = model
        self._url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

    async def complete(self, messages: list[dict[str, Any]]) -> str:
        async with httpx.AsyncClient(timeout=120.0) as client:
            _LOG.info("llm request provider=yandex messages=%s", _logged_messages(messages))
            r = await client.post(
                self._url,
                headers={"Authorization": f"Api-Key {self._api_key}"},
                json={
                    "modelUri": f"gpt://{self._catalog_id}/{self._model}",
                    "completionOptions": {"stream": False},
                    "messages": [{"role": m["role"], "text": _msg_text(m)} for m in messages],
                },
            )
            r.raise_for_status()
            data = r.json()
            text = _extract_yandex_text(data)
            _LOG.info("llm response provider=yandex text=%s", _truncate_text(text))
            return text


class AnthropicLLM:
    """Claude Messages API: https://docs.anthropic.com/claude/reference/messages_post"""

    def __init__(
        self,
        api_key: str,
        model: str,
        api_version: str = "2023-06-01",
        max_tokens: int = 8192,
    ):
        self._api_key = api_key
        self._model = model
        self._api_version = api_version
        self._max_tokens = max_tokens
        self._url = "https://api.anthropic.com/v1/messages"

    async def complete(self, messages: list[dict[str, Any]]) -> str:
        system_parts: list[str] = []
        conv: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role", "user")
            text = _msg_text(m)
            if role == "system":
                system_parts.append(text)
                continue
            if role not in ("user", "assistant"):
                role = "user"
            conv.append({"role": role, "content": text})
        _LOG.info(
            "llm request provider=anthropic system=%s messages=%s",
            _truncate_text("\n\n".join(system_parts)),
            _truncate_text(_safe_json(conv)),
        )
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": conv,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if not conv:
            conv = [{"role": "user", "content": ""}]

        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                self._url,
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": self._api_version,
                    "content-type": "application/json",
                },
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
        blocks = data.get("content") or []
        parts: list[str] = []
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                parts.append(b["text"])
        if parts:
            text = "\n".join(parts)
        else:
            text = _extract_text_from_value(data)
        _LOG.info("llm response provider=anthropic text=%s", _truncate_text(text))
        return text


class OpenAICompatibleLLM:
    """Любой endpoint с POST /v1/chat/completions (OpenAI, OpenRouter, vLLM, Azure и т.д.)."""

    def __init__(self, api_key: str, base_url: str, model: str):
        self._api_key = api_key
        base = base_url.rstrip("/")
        self._url = f"{base}/chat/completions"
        self._model = model

    async def complete(self, messages: list[dict[str, Any]]) -> str:
        body = {
            "model": self._model,
            "messages": [{"role": m.get("role", "user"), "content": _msg_text(m)} for m in messages],
        }
        _LOG.info("llm request provider=openai messages=%s", _logged_messages(messages))
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(self._url, headers=headers, json=body)
            r.raise_for_status()
            data = r.json()
        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    texts.append(part.get("text") or "")
            if texts:
                return "\n".join(texts)
        text = _extract_text_from_value(msg)
        if text:
            _LOG.info("llm response provider=openai text=%s", _truncate_text(text))
            return text
        text = _extract_text_from_value(data)
        _LOG.info("llm response provider=openai text=%s", _truncate_text(text))
        return text


def build_llm(config: AppConfig) -> LLM:
    provider = (config.llm_provider or "yandex").strip().lower()
    if provider in ("yandex", "yc", "yandexgpt"):
        return YandexLLM(
            config.yandex_api_key or "",
            config.yandex_catalog_id or "",
            config.yandex_model,
        )
    if provider in ("anthropic", "claude"):
        return AnthropicLLM(
            config.anthropic_api_key or "",
            model=config.anthropic_model,
            api_version=config.anthropic_api_version,
            max_tokens=config.anthropic_max_tokens,
        )
    if provider in ("openai", "openai_compatible", "compatible"):
        return OpenAICompatibleLLM(
            config.openai_api_key or "",
            base_url=config.openai_base_url,
            model=config.openai_model,
        )
    raise ValueError(
        f"Неизвестный LLM_PROVIDER={provider!r}. Допустимо: yandex, anthropic, openai"
    )
