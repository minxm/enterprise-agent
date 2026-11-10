package com.example.enterprise.integration.agent;

/**
 * Python {@code GET /api/v1/health} 的响应体。
 * 当前字段为 status 与 service。
 */
public record AgentHealthResponse(String status, String service) {
}
