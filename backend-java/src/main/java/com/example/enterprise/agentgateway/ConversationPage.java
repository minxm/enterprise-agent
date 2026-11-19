package com.example.enterprise.agentgateway;

import java.util.List;

public record ConversationPage(List<ConversationSummary> items, int page, int size, long total) {
}
