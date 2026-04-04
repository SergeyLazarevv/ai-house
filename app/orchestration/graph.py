"""Сборка и компиляция графа LangGraph: цикл supervisor → специалист → supervisor, затем synthesize."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.orchestration.agent_registry import SPECIALIST_SPECS
from app.orchestration.nodes import make_specialist_node, node_synthesize
from app.orchestration.state import GraphState
from app.orchestration.supervisor import (
    node_supervisor,
    route_after_sup_agent,
    route_after_supervisor,
)


def build_graph() -> StateGraph:
    g = StateGraph(GraphState)

    g.add_node("supervisor", node_supervisor)
    for spec in SPECIALIST_SPECS:
        g.add_node(spec.node_name, make_specialist_node(spec.role))
    g.add_node("synthesize", node_synthesize)

    g.add_edge(START, "supervisor")
    route_map = {spec.role: spec.node_name for spec in SPECIALIST_SPECS}
    route_map.update({"finish": "synthesize", "end": END})
    g.add_conditional_edges(
        "supervisor",
        route_after_supervisor,
        route_map,
    )

    for spec in SPECIALIST_SPECS:
        g.add_conditional_edges(
            spec.node_name,
            route_after_sup_agent,
            {"supervisor": "supervisor", "synthesize": "synthesize"},
        )

    g.add_edge("synthesize", END)

    return g


_compiled = None


def get_compiled_graph():
    global _compiled
    if _compiled is None:
        _compiled = build_graph().compile()
    return _compiled
