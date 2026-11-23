package com.example.enterprise.agentgateway;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;
import java.time.Instant;

@JsonIgnoreProperties(ignoreUnknown = false)
public record UpdateMemoryRequest(
        @NotNull @Min(1) Integer expectedVersion,
        @NotBlank @Size(max = 1000) String content,
        Instant expiresAt) {
}
