package com.example.enterprise.contract;

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
public class ContractExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(ContractExceptionHandler.class);

    @ExceptionHandler(ContractException.class)
    public ResponseEntity<ApiResponse<Void>> handle(ContractException exception) {
        log.warn("Contract request rejected: {}", exception.getMessage());
        return ResponseEntity.status(exception.status())
                .body(ApiResponse.failure(exception.status().value(), exception.getMessage()));
    }
}
