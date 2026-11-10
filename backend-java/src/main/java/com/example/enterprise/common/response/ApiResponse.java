package com.example.enterprise.common.response;

/**
 * 浏览器侧统一响应。code 使用 HTTP 语义状态码，不向前端返回异常堆栈。
 */
public record ApiResponse<T>(int code, String message, T data) {

    public static <T> ApiResponse<T> success(T data) {
        return new ApiResponse<>(200, "success", data);
    }

    public static <T> ApiResponse<T> failure(int code, String message) {
        return new ApiResponse<>(code, message, null);
    }
}
