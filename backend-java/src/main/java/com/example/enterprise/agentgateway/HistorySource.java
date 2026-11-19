package com.example.enterprise.agentgateway;

import com.fasterxml.jackson.annotation.JsonAlias;

public record HistorySource(@JsonAlias("chunk_id") String chunkId, String content) {
}
