"""Точка входа FastAPI: чат через граф LangGraph."""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.config import AppConfig
from app.graph_entry import run_user_request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ai_house")

_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)

app = FastAPI(title="ai-house", description="Мультиагенты: логи, БД, GitLab (LangGraph)")


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    response: str


class OpenAIChatMessage(BaseModel):
    role: str
    content: Any = None


class OpenAIChatRequest(BaseModel):
    model: str | None = None
    messages: list[OpenAIChatMessage]
    stream: bool | None = False


@app.get("/")
async def root():
    return RedirectResponse(url="/docs")


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    msg = (req.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Сообщение не может быть пустым")
    config = AppConfig.from_env()
    try:
        response = await run_user_request(msg, config)
        return ChatResponse(response=response)
    except Exception as e:
        log.exception("Ошибка обработки запроса")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/status")
async def status():
    config = AppConfig.from_env()
    return {
        "graylog": "disabled" if not config.graylog.enabled else ("ok" if config.graylog.is_configured else "не настроен"),
        "postgres": "disabled" if not config.postgres.enabled else ("ok" if config.postgres.is_configured else "не настроен"),
        "gitlab": "disabled" if not config.gitlab.enabled else ("ok" if config.gitlab.is_configured else "не настроен"),
        "llm": config.llm_status(),
        "llm_provider": config.llm_provider,
        "general": (
            "disabled"
            if not config.general_enabled
            else ("ok" if config.llm_status() == "ok" else "нужен LLM")
        ),
        "graph": "langgraph",
        "orchestrator": "llm_supervisor",
    }


@app.get("/v1/models")
async def openai_models():
    return {
        "object": "list",
        "data": [
            {
                "id": "ai-house-default",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "ai-house",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def openai_chat_completions(req: OpenAIChatRequest):
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages не может быть пустым")

    def _content_to_text(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                    continue
                if isinstance(item, dict):
                    if item.get("type") == "text" and isinstance(item.get("text"), str):
                        parts.append(item["text"])
                    elif isinstance(item.get("content"), str):
                        parts.append(item["content"])
            return "\n".join(p for p in parts if p)
        if isinstance(content, dict):
            if isinstance(content.get("text"), str):
                return content["text"]
            if isinstance(content.get("content"), str):
                return content["content"]
        return str(content)

    user_messages = [_content_to_text(m.content) for m in req.messages if m.role == "user" and m.content]
    if user_messages:
        prompt = user_messages[-1].strip()
    else:
        joined = "\n".join(_content_to_text(m.content) for m in req.messages).strip()
        prompt = joined

    if not prompt:
        raise HTTPException(status_code=400, detail="Не удалось извлечь текст запроса из messages")

    config = AppConfig.from_env()
    try:
        answer = await run_user_request(prompt, config)
    except Exception as e:
        log.exception("Ошибка OpenAI-совместимого endpoint")
        raise HTTPException(status_code=500, detail=str(e))

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    model = req.model or "ai-house-default"
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
