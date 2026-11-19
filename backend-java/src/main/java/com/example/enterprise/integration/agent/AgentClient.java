package com.example.enterprise.integration.agent;

import com.example.enterprise.agentgateway.HistoryMessage;
import com.example.enterprise.agentgateway.HistoryMessages;
import com.example.enterprise.common.request.RequestIds;
import com.example.enterprise.config.AgentServiceProperties;
import com.example.enterprise.internal.ActorTicketStore;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.ConnectException;
import java.net.NoRouteToHostException;
import java.net.SocketTimeoutException;
import java.net.UnknownHostException;
import java.net.http.HttpConnectTimeoutException;
import java.net.http.HttpTimeoutException;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

@Component
public class AgentClient {

    private static final Logger log = LoggerFactory.getLogger(AgentClient.class);
    private static final String INTERNAL_TOKEN_HEADER = "X-Agent-Token";

    private final RestClient agentRestClient;
    private final RestClient agentStreamRestClient;
    private final AgentServiceProperties properties;
    private final ActorTicketStore tickets;

    public AgentClient(
            @Qualifier("agentRestClient") RestClient agentRestClient,
            @Qualifier("agentStreamRestClient") RestClient agentStreamRestClient,
            AgentServiceProperties properties,
            ActorTicketStore tickets) {
        this.agentRestClient = agentRestClient;
        this.agentStreamRestClient = agentStreamRestClient;
        this.properties = properties;
        this.tickets = tickets;
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

    public List<HistoryMessage> readKnowledgeQaHistory(UUID tenantId, UUID actorId, UUID conversationId) {
        String token = properties.getInternalToken();
        if (token == null || token.isBlank()) {
            throw new AgentServiceException(HttpStatus.SERVICE_UNAVAILABLE, "Agent 服务认证未配置");
        }
        try {
            return agentRestClient.get()
                    .uri(uriBuilder -> uriBuilder
                            .path(properties.getKnowledgeQaHistoryPath())
                            .queryParam("tenant_id", tenantId)
                            .queryParam("actor_id", actorId)
                            .build(conversationId))
                    .header(RequestIds.HEADER, RequestIds.current().orElseGet(RequestIds::newId))
                    .header(INTERNAL_TOKEN_HEADER, token)
                    .exchange((request, response) -> {
                        int status = response.getStatusCode().value();
                        if (status == 404) {
                            throw new AgentServiceException(HttpStatus.NOT_FOUND, "会话不存在");
                        }
                        if (status == 409) {
                            throw new AgentServiceException(HttpStatus.CONFLICT, "同一个会话正在执行，请等待本次完成后再继续。");
                        }
                        if (status == 503) {
                            throw new AgentServiceException(HttpStatus.SERVICE_UNAVAILABLE, "工作流状态暂时不可用");
                        }
                        if (!response.getStatusCode().is2xxSuccessful()) {
                            throw upstreamStatus(status);
                        }
                        HistoryMessages body = response.bodyTo(HistoryMessages.class);
                        if (body == null || body.messages() == null) {
                            throw new AgentServiceException(HttpStatus.BAD_GATEWAY, "Agent 服务响应无法解析");
                        }
                        return body.messages();
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

    public void writeKnowledgeQaStream(
            UUID tenantId,
            UUID actorId,
            UUID conversationId,
            String question,
            Integer topK,
            String retrievalStrategy,
            String requestId,
            HttpServletResponse servletResponse) {
        String token = properties.getInternalToken();
        if (token == null || token.isBlank()) {
            throw new AgentServiceException(HttpStatus.SERVICE_UNAVAILABLE, "Agent 服务认证未配置");
        }
        String actorTicket = tickets.issue(tenantId, actorId);
        KnowledgeQaPythonRequest body = new KnowledgeQaPythonRequest(
                tenantId, actorId, conversationId, question, topK, retrievalStrategy, actorTicket);
        try {
            agentStreamRestClient.post()
                    .uri(properties.getKnowledgeQaPath())
                    .header(RequestIds.HEADER, requestId)
                    .header(INTERNAL_TOKEN_HEADER, token)
                    .header(HttpHeaders.ACCEPT, MediaType.TEXT_EVENT_STREAM_VALUE)
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(body)
                    .exchange((request, response) -> {
                        if (!response.getStatusCode().is2xxSuccessful()) {
                            discard(response.getBody());
                            throw upstreamStatus(response.getStatusCode().value());
                        }
                        copyStream(response.getBody(), servletResponse, requestId);
                        return null;
                    });
        } catch (AgentServiceException exception) {
            throw exception;
        } catch (RestClientException exception) {
            if (exception.getCause() instanceof AgentServiceException agentFailure) {
                throw agentFailure;
            }
            throw mapTransportFailure(exception);
        } finally {
            tickets.revoke(actorTicket);
        }
    }

    private void copyStream(InputStream input, HttpServletResponse servletResponse, String requestId) {
        try (InputStream source = input) {
            byte[] buffer = new byte[512];
            int read = readUpstream(source, buffer, servletResponse, requestId);
            if (read == -1) {
                if (!servletResponse.isCommitted()) {
                    prepareSse(servletResponse, requestId);
                }
                return;
            }
            prepareSse(servletResponse, requestId);
            OutputStream output = servletResponse.getOutputStream();
            while (read != -1) {
                if (!writeClient(output, servletResponse, buffer, read, requestId)) {
                    return;
                }
                read = readUpstream(source, buffer, servletResponse, requestId);
            }
        } catch (IOException exception) {
            if (servletResponse.isCommitted()) {
                log.info("Knowledge QA client disconnected requestId={}", requestId);
                return;
            }
            throw new AgentServiceException(HttpStatus.BAD_GATEWAY, "Agent 服务响应无法解析", exception);
        }
    }

    private int readUpstream(
            InputStream source, byte[] buffer, HttpServletResponse servletResponse, String requestId) {
        try {
            return source.read(buffer);
        } catch (IOException exception) {
            if (servletResponse.isCommitted()) {
                finishBrokenStream(servletResponse, requestId);
                return -1;
            }
            throw new AgentServiceException(HttpStatus.BAD_GATEWAY, "Agent 服务响应无法解析", exception);
        }
    }

    private boolean writeClient(
            OutputStream output,
            HttpServletResponse servletResponse,
            byte[] buffer,
            int read,
            String requestId) throws IOException {
        try {
            output.write(buffer, 0, read);
            output.flush();
            servletResponse.flushBuffer();
            return true;
        } catch (IOException exception) {
            if (servletResponse.isCommitted()) {
                log.info("Knowledge QA client disconnected requestId={}", requestId);
                return false;
            }
            throw exception;
        }
    }

    private void finishBrokenStream(HttpServletResponse servletResponse, String requestId) {
        try {
            OutputStream output = servletResponse.getOutputStream();
            String payload = "event: run_failed\ndata: {\"error_code\":\"agent_stream_interrupted\",\"safe_error_message\":\"Agent 服务响应中断\",\"request_id\":"
                    + quote(requestId)
                    + "}\n\n";
            output.write(payload.getBytes(StandardCharsets.UTF_8));
            output.flush();
            servletResponse.flushBuffer();
        } catch (IOException exception) {
            log.info("Knowledge QA client disconnected requestId={}", requestId);
        }
    }

    private static String quote(String value) {
        if (value == null) {
            return "\"\"";
        }
        StringBuilder builder = new StringBuilder(value.length() + 2);
        builder.append('"');
        for (int i = 0; i < value.length(); i++) {
            char current = value.charAt(i);
            if (current == '\\' || current == '"') {
                builder.append('\\').append(current);
            } else if (current < 0x20) {
                builder.append(' ');
            } else {
                builder.append(current);
            }
        }
        return builder.append('"').toString();
    }

    private static void prepareSse(HttpServletResponse servletResponse, String requestId) {
        servletResponse.setStatus(HttpServletResponse.SC_OK);
        servletResponse.setCharacterEncoding(StandardCharsets.UTF_8.name());
        servletResponse.setContentType("text/event-stream;charset=UTF-8");
        servletResponse.setHeader(HttpHeaders.CACHE_CONTROL, "no-cache, no-transform");
        servletResponse.setHeader(HttpHeaders.CONNECTION, "keep-alive");
        servletResponse.setHeader("X-Accel-Buffering", "no");
        servletResponse.setHeader(RequestIds.HEADER, requestId);
    }

    private static AgentServiceException upstreamStatus(int status) {
        if (status == 401) {
            return new AgentServiceException(HttpStatus.BAD_GATEWAY, "Agent 服务认证失败");
        }
        if (status == 422) {
            return new AgentServiceException(HttpStatus.BAD_REQUEST, "请求参数不合法");
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
