package com.example.enterprise.common.response;

import com.example.enterprise.common.request.RequestIds;

/**
 * 浏览器侧统一响应。code 使用 HTTP 语义状态码，不向前端返回异常堆栈。
 * requestId 与响应头 X-Request-ID 相同；没有请求上下文时为空。
 */
public record ApiResponse<T>(int code, String message, T data, String requestId) {

    public static <T> ApiResponse<T> success(T data) {
        return new ApiResponse<>(200, "success", data, RequestIds.current().orElse(null));
    }

    public static <T> ApiResponse<T> failure(int code, String message) {
        return new ApiResponse<>(code, message, null, RequestIds.current().orElse(null));
    }
}
