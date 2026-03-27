"""Сборка и компиляция графа LangGraph."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.orchestration.nodes import (
    node_inv_code,
    node_inv_db,
    node_inv_db_logs_pipeline,
    node_inv_logs,
    node_router,
    node_run_code,
    node_run_db,
    node_run_general,
    node_run_logs,
    node_run_logs_chain,
    node_synthesize,
    node_unknown,
    route_after_router,
)
from app.orchestration.state import GraphState


def build_graph() -> StateGraph:
    g = StateGraph(GraphState)

    g.add_node("router", node_router)
    g.add_node("run_logs", node_run_logs)
    g.add_node("run_db", node_run_db)
    g.add_node("run_code", node_run_code)
    g.add_node("run_logs_chain", node_run_logs_chain)
    g.add_node("inv_db", node_inv_db)
    g.add_node("inv_db_logs_pipeline", node_inv_db_logs_pipeline)
    g.add_node("inv_logs", node_inv_logs)
    g.add_node("inv_code", node_inv_code)
    g.add_node("synthesize", node_synthesize)
    g.add_node("run_general", node_run_general)
    g.add_node("run_unknown", node_unknown)

    g.add_edge(START, "router")
    g.add_conditional_edges(
        "router",
        route_after_router,
        {
            "logs": "run_logs",
            "db": "run_db",
            "code": "run_code",
            "logs_chain": "run_logs_chain",
            "investigate": "inv_db",
            "investigate_db_logs": "inv_db_logs_pipeline",
            "general": "run_general",
            "unknown": "run_unknown",
        },
    )

    g.add_edge("run_general", END)
    g.add_edge("run_logs", END)
    g.add_edge("run_db", END)
    g.add_edge("run_code", END)
    g.add_edge("run_logs_chain", END)
    g.add_edge("run_unknown", END)

    g.add_edge("inv_db_logs_pipeline", "synthesize")
    g.add_edge("inv_db", "inv_logs")
    g.add_edge("inv_logs", "inv_code")
    g.add_edge("inv_code", "synthesize")
    g.add_edge("synthesize", END)

    return g


_compiled = None


def get_compiled_graph():
    global _compiled
    if _compiled is None:
        _compiled = build_graph().compile()
    return _compiled
