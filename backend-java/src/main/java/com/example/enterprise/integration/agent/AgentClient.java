package com.example.enterprise.integration.agent;

import com.example.enterprise.common.request.RequestIds;
import com.example.enterprise.config.AgentServiceProperties;
import java.net.ConnectException;
import java.net.NoRouteToHostException;
import java.net.SocketTimeoutException;
import java.net.UnknownHostException;
import java.net.http.HttpConnectTimeoutException;
import java.net.http.HttpTimeoutException;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

@Component
public class AgentClient {

    private final RestClient agentRestClient;
    private final AgentServiceProperties properties;

    public AgentClient(RestClient agentRestClient, AgentServiceProperties properties) {
        this.agentRestClient = agentRestClient;
        this.properties = properties;
    }

    public AgentHealthResponse health() {
        try {
            String requestId = RequestIds.current().orElseGet(RequestIds::newId);
            return agentRestClient.get()
                    .uri(properties.getHealthPath())
                    .header(RequestIds.HEADER, requestId)
                    .exchange((request, response) -> {
                        if (!response.getStatusCode().is2xxSuccessful()) {
                            throw new AgentServiceException(
                                    HttpStatus.BAD_GATEWAY,
                                    "Agent 服务返回异常状态");
                        }
                        AgentHealthResponse body = response.bodyTo(AgentHealthResponse.class);
                        if (body == null) {
                            throw new AgentServiceException(
                                    HttpStatus.BAD_GATEWAY,
                                    "Agent 服务响应无法解析");
                        }
                        return body;
                    });
        } catch (AgentServiceException exception) {
            throw exception;
        } catch (RestClientException exception) {
            throw mapTransportFailure(exception);
        }
    }

    private AgentServiceException mapTransportFailure(RestClientException exception) {
        if (isReadTimeout(exception)) {
            return new AgentServiceException(HttpStatus.GATEWAY_TIMEOUT, "Agent 服务响应超时", exception);
        }
        if (isConnectivityFailure(exception)) {
            return new AgentServiceException(HttpStatus.SERVICE_UNAVAILABLE, "Agent 服务不可用", exception);
        }
        return new AgentServiceException(HttpStatus.BAD_GATEWAY, "Agent 服务响应无法解析", exception);
    }

    private boolean isReadTimeout(Throwable exception) {
        Throwable current = exception;
        while (current != null) {
            if (current instanceof HttpConnectTimeoutException || isConnectTimeout(current)) {
                return false;
            }
            if (current instanceof SocketTimeoutException || current instanceof HttpTimeoutException) {
                return true;
            }
            current = current.getCause();
        }
        return false;
    }

    private boolean isConnectivityFailure(Throwable exception) {
        Throwable current = exception;
        while (current != null) {
            if (current instanceof ConnectException
                    || current instanceof UnknownHostException
                    || current instanceof NoRouteToHostException
                    || current instanceof HttpConnectTimeoutException
                    || isConnectTimeout(current)) {
                return true;
            }
            current = current.getCause();
        }
        return false;
    }

    private boolean isConnectTimeout(Throwable exception) {
        if (!(exception instanceof SocketTimeoutException)) {
            return false;
        }
        String message = exception.getMessage();
        return message != null && message.toLowerCase().contains("connect");
    }
}
