package com.example.enterprise.identity;

import com.example.enterprise.common.response.ApiResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

@RestControllerAdvice
@Order(Ordered.HIGHEST_PRECEDENCE)
public class IdentityExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(IdentityExceptionHandler.class);

    @ExceptionHandler(IdentityException.class)
    public ResponseEntity<ApiResponse<Void>> handle(IdentityException exception) {
        log.warn("Identity request rejected: {}", exception.getMessage());
        return ResponseEntity.status(exception.status())
                .body(ApiResponse.failure(exception.status().value(), exception.getMessage()));
    }
}
