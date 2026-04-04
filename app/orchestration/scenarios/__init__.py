"""Загрузка сценариев оркестратора из отдельных файлов."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


_SCENARIOS_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def load_scenarios_text() -> str:
    parts: list[str] = []
    for path in sorted(_SCENARIOS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            continue
        parts.append(f"### {path.stem}\n{text}")
    return "\n\n".join(parts).strip()
