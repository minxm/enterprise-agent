package com.example.enterprise.config;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import java.time.Duration;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.validation.annotation.Validated;

@Validated
@ConfigurationProperties(prefix = "agent.service")
public class AgentServiceProperties {

    @NotBlank
    private String baseUrl;

    @NotBlank
    private String healthPath;

    @NotNull
    private Duration connectTimeout = Duration.ofSeconds(3);

    @NotNull
    private Duration readTimeout = Duration.ofSeconds(5);

    @NotBlank
    private String knowledgeQaPath = "/internal/v1/knowledge-qa/stream";

    @NotBlank
    private String knowledgeQaHistoryPath = "/internal/v1/knowledge-qa/conversations/{conversationId}/messages";

    @NotBlank
    private String memoryPath = "/internal/v1/memories";

    @NotNull
    private Duration streamReadTimeout = Duration.ofSeconds(60);

    private String internalToken = "";

    public String getBaseUrl() {
        return baseUrl;
    }

    public void setBaseUrl(String baseUrl) {
        this.baseUrl = baseUrl;
    }

    public String getHealthPath() {
        return healthPath;
    }

    public void setHealthPath(String healthPath) {
        this.healthPath = healthPath;
    }

    public Duration getConnectTimeout() {
        return connectTimeout;
    }

    public void setConnectTimeout(Duration connectTimeout) {
        this.connectTimeout = connectTimeout;
    }

    public Duration getReadTimeout() {
        return readTimeout;
    }

    public void setReadTimeout(Duration readTimeout) {
        this.readTimeout = readTimeout;
    }

    public String getKnowledgeQaPath() {
        return knowledgeQaPath;
    }

    public void setKnowledgeQaPath(String knowledgeQaPath) {
        this.knowledgeQaPath = knowledgeQaPath;
    }

    public String getKnowledgeQaHistoryPath() {
        return knowledgeQaHistoryPath;
    }

    public void setKnowledgeQaHistoryPath(String knowledgeQaHistoryPath) {
        this.knowledgeQaHistoryPath = knowledgeQaHistoryPath;
    }

    public String getMemoryPath() {
        return memoryPath;
    }

    public void setMemoryPath(String memoryPath) {
        this.memoryPath = memoryPath;
    }

    public Duration getStreamReadTimeout() {
        return streamReadTimeout;
    }

    public void setStreamReadTimeout(Duration streamReadTimeout) {
        this.streamReadTimeout = streamReadTimeout;
    }

    public String getInternalToken() {
        return internalToken;
    }

    public void setInternalToken(String internalToken) {
        this.internalToken = internalToken;
    }
}
