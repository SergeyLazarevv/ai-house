"""Провайдеры LLM: Yandex, Anthropic (Claude), OpenAI-совместимый API. Выбор через LLM_PROVIDER в env."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import httpx

from app.config import AppConfig


def _msg_text(m: dict[str, Any]) -> str:
    c = m.get("content", m.get("text"))
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    return str(c)


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
            return data["result"]["alternatives"][0]["message"]["text"]


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
        return "\n".join(parts) if parts else ""


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
            return "\n".join(texts)
        return str(content) if content is not None else ""


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
