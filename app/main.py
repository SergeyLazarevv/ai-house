"""Точка входа FastAPI: чат через граф LangGraph."""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel

from app.config import AppConfig
from app.graph_entry import run_user_request
from app.shared.llm import build_llm

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


def _is_ui_meta_request(prompt: str) -> bool:
    text = (prompt or "").strip()
    if not text:
        return False
    lower = text.lower()
    strong_markers = (
        "suggest 3-5 relevant follow-up questions",
        "### chat history:",
        'json format: { "follow_ups":',
        '"follow_ups":',
    )
    weak_markers = (
        "### task:",
        "follow-up questions",
        "chat history",
        "response must be a json array of strings",
    )
    if any(marker in lower for marker in strong_markers):
        return True
    weak_hits = sum(1 for marker in weak_markers if marker in lower)
    return weak_hits >= 2


async def _run_meta_request(prompt: str, config: AppConfig) -> str:
    llm = build_llm(config)
    log.info("meta request bypassed graph")
    return (await llm.complete([{"role": "user", "content": prompt}])).strip()


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
        if _is_ui_meta_request(msg):
            response = await _run_meta_request(msg, config)
        else:
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
            return ""
        if isinstance(content, (int, float, bool)):
            return str(content)
        return ""

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
        if _is_ui_meta_request(prompt):
            answer = await _run_meta_request(prompt, config)
        else:
            answer = await run_user_request(prompt, config)
    except Exception as e:
        log.exception("Ошибка OpenAI-совместимого endpoint")
        raise HTTPException(status_code=500, detail=str(e))

    answer_text = _content_to_text(answer).strip()
    if not answer_text:
        answer_text = "Извините, не удалось сформировать текстовый ответ."

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    model = req.model or "ai-house-default"
    if req.stream:
        async def _event_stream():
            first_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant"},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n"

            content_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": answer_text},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(content_chunk, ensure_ascii=False)}\n\n"

            final_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
            }
            yield f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": answer_text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
