package com.example.enterprise.integration.agent;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.UUID;

@JsonInclude(JsonInclude.Include.NON_NULL)
public record KnowledgeQaPythonRequest(
        @JsonProperty("conversation_id") UUID conversationId,
        String question,
        @JsonProperty("top_k") Integer topK,
        @JsonProperty("retrieval_strategy") String retrievalStrategy,
        @JsonProperty("actor_ticket") String actorTicket,
        @JsonProperty("knowledge_qa_ticket") String knowledgeQaTicket) {
}
