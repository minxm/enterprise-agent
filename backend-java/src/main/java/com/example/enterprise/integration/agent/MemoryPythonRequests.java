package com.example.enterprise.integration.agent;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.time.Instant;
import java.util.UUID;

@JsonInclude(JsonInclude.Include.NON_NULL)
record MemoryCallerPayload(
        @JsonProperty("tenant_id") UUID tenantId,
        @JsonProperty("actor_id") UUID actorId,
        @JsonProperty("allow_tenant_read") boolean allowTenantRead,
        @JsonProperty("allow_tenant_write") boolean allowTenantWrite) {
}

@JsonInclude(JsonInclude.Include.NON_NULL)
record MemoryCreatePayload(
        MemoryCallerPayload caller,
        String scope,
        @JsonProperty("memory_type") String memoryType,
        String content,
        @JsonProperty("expires_at") Instant expiresAt,
        @JsonProperty("source_conversation_id") UUID sourceConversationId) {
}

@JsonInclude(JsonInclude.Include.NON_NULL)
record MemoryUpdatePayload(
        MemoryCallerPayload caller,
        @JsonProperty("expected_version") int expectedVersion,
        String content,
        @JsonProperty("expires_at") Instant expiresAt) {
}

record MemoryVersionPayload(
        MemoryCallerPayload caller,
        @JsonProperty("expected_version") int expectedVersion) {
}
