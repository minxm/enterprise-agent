package com.example.enterprise.common.response;

import com.example.enterprise.integration.agent.AgentServiceException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.method.annotation.MethodArgumentTypeMismatchException;
import org.springframework.web.multipart.MaxUploadSizeExceededException;
import org.springframework.web.multipart.MultipartException;
import org.springframework.web.servlet.resource.NoResourceFoundException;

@RestControllerAdvice
public class GlobalExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(GlobalExceptionHandler.class);

    @ExceptionHandler(AgentServiceException.class)
    public ResponseEntity<ApiResponse<Void>> handleAgentService(AgentServiceException exception) {
        log.warn("Agent dependency call failed: {}", exception.getMessage());
        return error(exception.getStatus(), exception.getMessage());
    }

    @ExceptionHandler({
            MethodArgumentNotValidException.class,
            HttpMessageNotReadableException.class,
            MethodArgumentTypeMismatchException.class
    })
    public ResponseEntity<ApiResponse<Void>> handleInvalidRequest(Exception exception) {
        log.warn("Rejected invalid request: {}", exception.getClass().getSimpleName());
        return error(HttpStatus.BAD_REQUEST, "请求参数不合法");
    }

    @ExceptionHandler(MultipartException.class)
    public ResponseEntity<ApiResponse<Void>> handleMultipart(MultipartException exception) {
        log.warn("Rejected upload: {}", exception.getClass().getSimpleName());
        if (exception instanceof MaxUploadSizeExceededException || exception.getCause() instanceof MaxUploadSizeExceededException) {
            return error(HttpStatus.BAD_REQUEST, "文件大小超出限制");
        }
        return error(HttpStatus.BAD_REQUEST, "请求参数不合法");
    }

    @ExceptionHandler(NoResourceFoundException.class)
    public ResponseEntity<ApiResponse<Void>> handleMissing(NoResourceFoundException exception) {
        log.warn("No matching resource");
        return error(HttpStatus.NOT_FOUND, "资源不存在");
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<ApiResponse<Void>> handleUnexpected(Exception exception) {
        log.error("Unhandled request failure", exception);
        return error(HttpStatus.INTERNAL_SERVER_ERROR, "服务内部错误");
    }

    private ResponseEntity<ApiResponse<Void>> error(HttpStatus status, String message) {
        return ResponseEntity.status(status).body(ApiResponse.failure(status.value(), message));
    }
}
