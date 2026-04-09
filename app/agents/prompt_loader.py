"""Загрузка системных промптов агентов из файлов."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_AGENTS_DIR = Path(__file__).resolve().parent


def _load_logs_prompt_parts() -> str:
    """Сборка промпта из app/agents/logs/prompt/01_*.txt … по порядку имени файла."""
    folder = _AGENTS_DIR / "logs" / "prompt"
    if not folder.is_dir():
        legacy = _AGENTS_DIR / "logs" / "prompt.txt"
        return legacy.read_text(encoding="utf-8").strip()
    chunks: list[str] = []
    # Только 01_…99_*.txt; служебные файлы вроде _STRUCTURE.txt не в промпт.
    for path in sorted(folder.glob("[0-9][0-9]_*.txt")):
        raw = path.read_text(encoding="utf-8").strip()
        if raw:
            chunks.append(raw)
    return "\n\n".join(chunks)


@lru_cache(maxsize=None)
def load_agent_prompt(role: str) -> str:
    if role == "logs":
        return _load_logs_prompt_parts()
    path = _AGENTS_DIR / role / "prompt.txt"
    return path.read_text(encoding="utf-8").strip()
