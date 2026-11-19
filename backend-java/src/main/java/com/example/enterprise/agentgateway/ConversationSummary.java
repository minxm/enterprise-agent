package com.example.enterprise.agentgateway;

import java.time.Instant;
import java.util.UUID;

public record ConversationSummary(UUID conversationId, Instant createdAt) {
}
