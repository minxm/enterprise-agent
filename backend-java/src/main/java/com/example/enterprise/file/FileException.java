package com.example.enterprise.file;

import org.springframework.http.HttpStatus;

public class FileException extends RuntimeException {

    private final HttpStatus status;

    public FileException(HttpStatus status, String message) {
        super(message);
        this.status = status;
    }

    public HttpStatus status() {
        return status;
    }
}
