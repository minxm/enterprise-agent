package com.example.enterprise.contract;

import org.springframework.http.HttpStatus;

public class ContractException extends RuntimeException {

    private final HttpStatus status;

    public ContractException(HttpStatus status, String message) {
        super(message);
        this.status = status;
    }

    public HttpStatus status() {
        return status;
    }
}
