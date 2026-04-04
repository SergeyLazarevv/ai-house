"""Загрузка системных промптов агентов из файлов."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=None)
def load_agent_prompt(role: str) -> str:
    path = Path(__file__).resolve().parent / role / "prompt.txt"
    return path.read_text(encoding="utf-8").strip()
