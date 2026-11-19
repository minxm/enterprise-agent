package com.example.enterprise.agentgateway;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;
import java.util.UUID;

@JsonIgnoreProperties(ignoreUnknown = true)
public record KnowledgeQaStreamRequest(
        @NotNull UUID conversationId,
        @NotBlank @Size(max = 2000) String question,
        @Min(1) @Max(20) Integer topK,
        @Pattern(regexp = "vector|hybrid|hybrid_rerank") String retrievalStrategy) {
}
