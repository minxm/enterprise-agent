package com.example.enterprise.common.request;

import java.util.Optional;
import java.util.UUID;
import java.util.regex.Pattern;
import org.slf4j.MDC;

/**
 * 本次请求的唯一标识。只接受安全字符，避免把任意请求头写入日志。
 */
public final class RequestIds {

    public static final String HEADER = "X-Request-ID";
    public static final String MDC_KEY = "requestId";
    private static final Pattern SAFE = Pattern.compile("[A-Za-z0-9_-]{1,128}");

    private RequestIds() {
    }

    public static String resolve(String incoming) {
        if (incoming != null && SAFE.matcher(incoming).matches()) {
            return incoming;
        }
        return newId();
    }

    public static String newId() {
        return UUID.randomUUID().toString();
    }

    public static Optional<String> current() {
        String value = MDC.get(MDC_KEY);
        if (value != null && SAFE.matcher(value).matches()) {
            return Optional.of(value);
        }
        return Optional.empty();
    }
}
