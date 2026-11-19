package com.example.enterprise.agentgateway;

import java.util.List;
import java.util.UUID;

public interface AgentConversationStore {

    UUID insert(UUID tenantId, UUID ownerUserId);

    boolean ownedBy(UUID tenantId, UUID ownerUserId, UUID conversationId);

    List<ConversationSummary> list(UUID tenantId, UUID ownerUserId, int limit, long offset);

    long count(UUID tenantId, UUID ownerUserId);
}
