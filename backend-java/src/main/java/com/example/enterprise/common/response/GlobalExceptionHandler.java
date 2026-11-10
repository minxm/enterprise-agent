package com.example.enterprise.common.response;

import com.example.enterprise.integration.agent.AgentServiceException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

@RestControllerAdvice
public class GlobalExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(GlobalExceptionHandler.class);

    @ExceptionHandler(AgentServiceException.class)
    public ResponseEntity<ApiResponse<Void>> handleAgentService(AgentServiceException exception) {
        log.warn("Agent dependency call failed: {}", exception.getMessage());
        HttpStatus status = exception.getStatus();
        return ResponseEntity.status(status)
                .body(ApiResponse.failure(status.value(), exception.getMessage()));
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<ApiResponse<Void>> handleUnexpected(Exception exception) {
        log.error("Unhandled request failure", exception);
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(ApiResponse.failure(HttpStatus.INTERNAL_SERVER_ERROR.value(), "服务内部错误"));
    }
}
