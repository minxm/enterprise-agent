package com.example.enterprise.agentgateway;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotNull;

@JsonIgnoreProperties(ignoreUnknown = false)
public record MemoryVersionRequest(@NotNull @Min(1) Integer expectedVersion) {
}
