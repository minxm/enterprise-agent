package com.example.enterprise.integration.agent;

import com.example.enterprise.agentgateway.MemoryPage;
import com.example.enterprise.agentgateway.MemoryView;
import com.example.enterprise.common.request.RequestIds;
import com.example.enterprise.config.AgentServiceProperties;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.ConnectException;
import java.net.NoRouteToHostException;
import java.net.SocketTimeoutException;
import java.net.UnknownHostException;
import java.net.http.HttpConnectTimeoutException;
import java.net.http.HttpTimeoutException;
import java.time.Instant;
import java.util.UUID;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;
import org.springframework.web.util.UriBuilder;

@Component
public class MemoryAgentClient {

    private static final String INTERNAL_TOKEN_HEADER = "X-Agent-Token";

    private final RestClient agentRestClient;
    private final AgentServiceProperties properties;

    public MemoryAgentClient(
            @Qualifier("agentRestClient") RestClient agentRestClient,
            AgentServiceProperties properties) {
        this.agentRestClient = agentRestClient;
        this.properties = properties;
    }

    public MemoryView create(
            UUID tenantId,
            UUID actorId,
            boolean allowTenantRead,
            boolean allowTenantWrite,
            String scope,
            String memoryType,
            String content,
            Instant expiresAt,
            UUID sourceConversationId) {
        MemoryCreatePayload body = new MemoryCreatePayload(
                caller(tenantId, actorId, allowTenantRead, allowTenantWrite),
                scope,
                memoryType,
                content,
                expiresAt,
                sourceConversationId);
        return exchange("POST", properties.getMemoryPath(), body, null, MemoryView.class);
    }

    public MemoryPage list(
            UUID tenantId,
            UUID actorId,
            boolean allowTenantRead,
            boolean allowTenantWrite,
            String scope,
            String memoryType,
            String status,
            boolean includeExpired) {
        return exchange(
                "GET",
                properties.getMemoryPath(),
                null,
                builder -> identity(builder, tenantId, actorId, allowTenantRead, allowTenantWrite)
                        .queryParamIfPresent("scope", optional(scope))
                        .queryParamIfPresent("memory_type", optional(memoryType))
                        .queryParamIfPresent("status", optional(status))
                        .queryParam("include_expired", includeExpired),
                MemoryPage.class);
    }

    public MemoryView get(
            UUID tenantId,
            UUID actorId,
            boolean allowTenantRead,
            boolean allowTenantWrite,
            UUID memoryId) {
        return exchange(
                "GET",
                properties.getMemoryPath() + "/" + memoryId,
                null,
                builder -> identity(builder, tenantId, actorId, allowTenantRead, allowTenantWrite),
                MemoryView.class);
    }

    public MemoryView update(
            UUID tenantId,
            UUID actorId,
            boolean allowTenantRead,
            boolean allowTenantWrite,
            UUID memoryId,
            int expectedVersion,
            String content,
            Instant expiresAt) {
        MemoryUpdatePayload body = new MemoryUpdatePayload(
                caller(tenantId, actorId, allowTenantRead, allowTenantWrite),
                expectedVersion,
                content,
                expiresAt);
        return exchange("PUT", properties.getMemoryPath() + "/" + memoryId, body, null, MemoryView.class);
    }

    public MemoryView archive(
            UUID tenantId,
            UUID actorId,
            boolean allowTenantRead,
            boolean allowTenantWrite,
            UUID memoryId,
            int expectedVersion) {
        return exchange(
                "POST",
                properties.getMemoryPath() + "/" + memoryId + "/archive",
                new MemoryVersionPayload(caller(tenantId, actorId, allowTenantRead, allowTenantWrite), expectedVersion),
                null,
                MemoryView.class);
    }

    public MemoryView delete(
            UUID tenantId,
            UUID actorId,
            boolean allowTenantRead,
            boolean allowTenantWrite,
            UUID memoryId,
            int expectedVersion) {
        return exchange(
                "DELETE",
                properties.getMemoryPath() + "/" + memoryId,
                new MemoryVersionPayload(caller(tenantId, actorId, allowTenantRead, allowTenantWrite), expectedVersion),
                null,
                MemoryView.class);
    }

    private <T> T exchange(
            String method,
            String path,
            Object body,
            java.util.function.Function<UriBuilder, UriBuilder> query,
            Class<T> type) {
        String token = properties.getInternalToken();
        if (token == null || token.isBlank()) {
            throw new AgentServiceException(HttpStatus.SERVICE_UNAVAILABLE, "Agent 服务认证未配置");
        }
        try {
            var spec = agentRestClient.method(org.springframework.http.HttpMethod.valueOf(method))
                    .uri(builder -> {
                        builder.path(path);
                        if (query != null) {
                            query.apply(builder);
                        }
                        return builder.build();
                    })
                    .header(RequestIds.HEADER, RequestIds.current().orElseGet(RequestIds::newId))
                    .header(INTERNAL_TOKEN_HEADER, token);
            if (body != null) {
                spec = spec.body(body);
            }
            return spec.exchange((request, response) -> {
                        int status = response.getStatusCode().value();
                        if (!response.getStatusCode().is2xxSuccessful()) {
                            discard(response.getBody());
                            throw mapped(status);
                        }
                        T parsed = response.bodyTo(type);
                        if (parsed == null) {
                            throw new AgentServiceException(HttpStatus.BAD_GATEWAY, "Agent 服务响应无法解析");
                        }
                        return parsed;
                    });
        } catch (AgentServiceException exception) {
            throw exception;
        } catch (RestClientException exception) {
            if (exception.getCause() instanceof AgentServiceException agentFailure) {
                throw agentFailure;
            }
            throw mapTransportFailure(exception);
        }
    }

    private static UriBuilder identity(
            UriBuilder builder,
            UUID tenantId,
            UUID actorId,
            boolean allowTenantRead,
            boolean allowTenantWrite) {
        return builder.queryParam("tenant_id", tenantId)
                .queryParam("actor_id", actorId)
                .queryParam("allow_tenant_read", allowTenantRead)
                .queryParam("allow_tenant_write", allowTenantWrite);
    }

    private static MemoryCallerPayload caller(
            UUID tenantId,
            UUID actorId,
            boolean allowTenantRead,
            boolean allowTenantWrite) {
        return new MemoryCallerPayload(tenantId, actorId, allowTenantRead, allowTenantWrite);
    }

    private static java.util.Optional<String> optional(String value) {
        if (value == null || value.isBlank()) {
            return java.util.Optional.empty();
        }
        return java.util.Optional.of(value);
    }

    private static AgentServiceException mapped(int status) {
        if (status == 401) {
            return new AgentServiceException(HttpStatus.BAD_GATEWAY, "Agent 服务认证失败");
        }
        if (status == 403) {
            return new AgentServiceException(HttpStatus.FORBIDDEN, "无权访问租户记忆");
        }
        if (status == 404) {
            return new AgentServiceException(HttpStatus.NOT_FOUND, "记忆不存在");
        }
        if (status == 409) {
            return new AgentServiceException(HttpStatus.CONFLICT, "记忆版本冲突");
        }
        if (status == 400 || status == 422) {
            return new AgentServiceException(HttpStatus.BAD_REQUEST, "请求参数不合法");
        }
        if (status == 503) {
            return new AgentServiceException(HttpStatus.SERVICE_UNAVAILABLE, "Agent 服务不可用");
        }
        return new AgentServiceException(HttpStatus.BAD_GATEWAY, "Agent 服务返回异常状态");
    }

    private static void discard(InputStream body) {
        if (body == null) {
            return;
        }
        try (InputStream source = body) {
            source.transferTo(OutputStream.nullOutputStream());
        } catch (IOException ignored) {
            // The error body is never logged or returned.
        }
    }

    private static AgentServiceException mapTransportFailure(RestClientException exception) {
        if (isConnectivityFailure(exception)) {
            return new AgentServiceException(HttpStatus.SERVICE_UNAVAILABLE, "Agent 服务不可用", exception);
        }
        Throwable current = exception;
        while (current != null) {
            if (current instanceof SocketTimeoutException || current instanceof HttpTimeoutException) {
                return new AgentServiceException(HttpStatus.GATEWAY_TIMEOUT, "Agent 服务响应超时", exception);
            }
            current = current.getCause();
        }
        return new AgentServiceException(HttpStatus.BAD_GATEWAY, "Agent 服务响应无法解析", exception);
    }

    private static boolean isConnectivityFailure(Throwable exception) {
        Throwable current = exception;
        while (current != null) {
            if (current instanceof ConnectException
                    || current instanceof UnknownHostException
                    || current instanceof NoRouteToHostException
                    || current instanceof HttpConnectTimeoutException) {
                return true;
            }
            if (current instanceof SocketTimeoutException timeout
                    && timeout.getMessage() != null
                    && timeout.getMessage().toLowerCase().contains("connect")) {
                return true;
            }
            current = current.getCause();
        }
        return false;
    }
}
