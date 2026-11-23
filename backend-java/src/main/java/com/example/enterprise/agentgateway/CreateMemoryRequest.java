package com.example.enterprise.agentgateway;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;
import java.time.Instant;
import java.util.UUID;

@JsonIgnoreProperties(ignoreUnknown = false)
public record CreateMemoryRequest(
        @NotNull @Pattern(regexp = "user|tenant") String scope,
        @NotNull @Pattern(regexp = "preference|format|work_scope|organization_practice") String memoryType,
        @NotBlank @Size(max = 1000) String content,
        Instant expiresAt,
        UUID sourceConversationId) {
}
