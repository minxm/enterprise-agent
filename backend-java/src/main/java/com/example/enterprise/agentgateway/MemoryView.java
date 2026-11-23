package com.example.enterprise.agentgateway;

import com.fasterxml.jackson.annotation.JsonAlias;
import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import java.time.Instant;
import java.util.UUID;

@JsonIgnoreProperties(ignoreUnknown = true)
public record MemoryView(
        UUID id,
        String scope,
        @JsonAlias("memory_type") String memoryType,
        String content,
        String source,
        @JsonAlias("source_conversation_id") UUID sourceConversationId,
        String status,
        @JsonAlias("created_at") Instant createdAt,
        @JsonAlias("updated_at") Instant updatedAt,
        @JsonAlias("expires_at") Instant expiresAt,
        @JsonAlias("deleted_at") Instant deletedAt,
        int version) {
}
