package com.example.enterprise.agentgateway;

import com.fasterxml.jackson.annotation.JsonAlias;
import java.util.List;

public record HistoryMessage(
        String role,
        String content,
        String status,
        @JsonAlias("source_ids") List<String> sourceIds,
        List<HistorySource> sources) {
}
