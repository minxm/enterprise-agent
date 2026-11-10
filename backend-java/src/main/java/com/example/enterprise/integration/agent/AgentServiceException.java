package com.example.enterprise.integration.agent;

import org.springframework.http.HttpStatus;

/**
 * Python Agent 依赖调用失败。Spring Boot 进程本身继续运行。
 */
public class AgentServiceException extends RuntimeException {

    private final HttpStatus status;

    public AgentServiceException(HttpStatus status, String message) {
        super(message);
        this.status = status;
    }

    public AgentServiceException(HttpStatus status, String message, Throwable cause) {
        super(message, cause);
        this.status = status;
    }

    public HttpStatus getStatus() {
        return status;
    }
}
