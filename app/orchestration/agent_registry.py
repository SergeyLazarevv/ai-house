"""Единый реестр специалистов для графа и оркестратора."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentSpec:
    role: str
    node_name: str
    result_slot: str
    disabled_message: str
    unavailable_message: str


SPECIALIST_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec(
        role="db",
        node_name="sup_db",
        result_slot="db_result",
        disabled_message="Агент БД отключен (AGENT_DB_ENABLED=false).",
        unavailable_message="Агент БД недоступен.",
    ),
    AgentSpec(
        role="logs",
        node_name="sup_logs",
        result_slot="logs_result",
        disabled_message="Агент логов отключен (AGENT_LOGS_ENABLED=false).",
        unavailable_message="Агент логов недоступен.",
    ),
    AgentSpec(
        role="code",
        node_name="sup_code",
        result_slot="code_result",
        disabled_message="Агент кода отключен (AGENT_CODE_ENABLED=false).",
        unavailable_message="Агент кода недоступен.",
    ),
    AgentSpec(
        role="general",
        node_name="sup_general",
        result_slot="final_response",
        disabled_message="Общий агент отключен (AGENT_GENERAL_ENABLED=false).",
        unavailable_message="Общий агент недоступен.",
    ),
)

SPECIALIST_BY_ROLE: dict[str, AgentSpec] = {spec.role: spec for spec in SPECIALIST_SPECS}
