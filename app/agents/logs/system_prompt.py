"""Сборка system prompt для агента логов."""

from __future__ import annotations

from app.agents.prompt_loader import load_agent_prompt
from app.shared.connectors.graylog import format_tools_for_llm

TOOL_CALL_FORMAT = """
## Формат вызова
`TOOL_CALL: <имя>` и следующей строкой JSON аргументов по схеме каталога.

Примеры:
TOOL_CALL: list_inputs
{}

TOOL_CALL: <tool_name_from_catalog>
{...}
"""


def build_logs_system_prompt(tools: list[dict]) -> str:
    parts = [
        load_agent_prompt("logs").strip(),
        TOOL_CALL_FORMAT.strip(),
        format_tools_for_llm(tools).strip(),
    ]
    return "\n\n".join(p for p in parts if p)
