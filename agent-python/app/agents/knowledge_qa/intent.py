"""Single intent rule shared by the service and the graph.

The service recalls memory before the graph starts. The graph classifies again
at route_intent. Both call this function so an explicit intent cannot take one
path for recall and another path inside the graph.
"""

from __future__ import annotations

from app.agents.knowledge_qa.contract_intent import looks_like_contract_query

RECALL_INTENTS = frozenset({"knowledge_qa", "contract_query"})


def classify_intent(requested: object, question: object) -> str:
    """Honor an explicit intent. Otherwise detect a contract lookup from the question."""
    if requested == "contract_query" or (requested is None and looks_like_contract_query(question)):
        return "contract_query"
    if requested in {None, "knowledge_qa"}:
        return "knowledge_qa"
    return "unsupported"
