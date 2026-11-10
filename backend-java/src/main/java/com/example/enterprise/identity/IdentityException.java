package com.example.enterprise.identity;

import org.springframework.http.HttpStatus;

public class IdentityException extends RuntimeException {

    private final HttpStatus status;

    public IdentityException(HttpStatus status, String message) {
        super(message);
        this.status = status;
    }

    public HttpStatus status() {
        return status;
    }
}
