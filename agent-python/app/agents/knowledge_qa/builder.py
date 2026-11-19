"""Build the Knowledge QA graph without executing it.

START -> validate_input -> route_intent -> knowledge_qa or unsupported -> format_response -> END

Later workflows can add nodes for contract approval, payment review, or human
approval by extending the route map. A checkpointer is optional. Day 26 tests
compile the graph without one.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.agents.knowledge_qa.nodes import format_response, knowledge_qa, route_intent, unsupported, validate_input
from app.agents.knowledge_qa.state import KnowledgeQAState


def build_knowledge_qa_graph(
    rag_service: object,
    *,
    default_top_k: int = 5,
    default_strategy: str = "vector",
    checkpointer: object | None = None,
):
    graph = StateGraph(KnowledgeQAState)
    graph.add_node("validate_input", lambda state: validate_input(state, default_top_k=default_top_k, default_strategy=default_strategy))
    graph.add_node("route_intent", route_intent)
    graph.add_node("knowledge_qa", lambda state: knowledge_qa(state, rag_service=rag_service))
    graph.add_node("unsupported", unsupported)
    graph.add_node("format_response", format_response)
    graph.add_edge(START, "validate_input")
    graph.add_conditional_edges(
        "validate_input",
        _after_validation,
        {"route_intent": "route_intent", "format_response": "format_response"},
    )
    graph.add_conditional_edges(
        "route_intent",
        _after_intent,
        {"knowledge_qa": "knowledge_qa", "unsupported": "unsupported"},
    )
    graph.add_edge("knowledge_qa", "format_response")
    graph.add_edge("unsupported", "format_response")
    graph.add_edge("format_response", END)
    if checkpointer is None:
        return graph.compile()
    return graph.compile(checkpointer=checkpointer)


def _after_validation(state: KnowledgeQAState) -> str:
    if state.get("status") == "invalid_input":
        return "format_response"
    return "route_intent"


def _after_intent(state: KnowledgeQAState) -> str:
    if state.get("intent") == "knowledge_qa":
        return "knowledge_qa"
    return "unsupported"
