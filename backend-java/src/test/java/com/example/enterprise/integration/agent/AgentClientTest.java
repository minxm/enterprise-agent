package com.example.enterprise.integration.agent;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.example.enterprise.agentgateway.HistorySource;
import com.example.enterprise.common.request.RequestIds;
import com.example.enterprise.config.AgentServiceProperties;
import com.example.enterprise.config.RestClientConfig;
import com.example.enterprise.internal.ActorTicketStore;
import jakarta.servlet.ServletOutputStream;
import jakarta.servlet.WriteListener;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import okhttp3.mockwebserver.MockResponse;
import okhttp3.mockwebserver.MockWebServer;
import okhttp3.mockwebserver.RecordedRequest;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.slf4j.MDC;
import org.springframework.http.HttpStatus;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.web.client.RestClient;

class AgentClientTest {

    private MockWebServer server;

    @BeforeEach
    void setUp() throws Exception {
        server = new MockWebServer();
        server.start();
    }

    @AfterEach
    void tearDown() throws Exception {
        server.shutdown();
    }

    @Test
    void parsesPythonHealthResponse() {
        server.enqueue(new MockResponse()
                .setHeader("Content-Type", "application/json")
                .setBody("{\"status\":\"ok\",\"service\":\"agent-runtime\"}"));

        AgentHealthResponse response = client(server.url("/").toString(), Duration.ofSeconds(1), Duration.ofSeconds(1))
                .health();

        assertThat(response.status()).isEqualTo("ok");
        assertThat(response.service()).isEqualTo("agent-runtime");
    }

    @Test
    void reportsUnavailableWhenConnectionFails() {
        AgentClient client = client("http://127.0.0.1:1", Duration.ofMillis(300), Duration.ofSeconds(1));

        assertThatThrownBy(client::health)
                .isInstanceOf(AgentServiceException.class)
                .hasMessage("Agent 服务不可用")
                .extracting(exception -> ((AgentServiceException) exception).getStatus())
                .isEqualTo(HttpStatus.SERVICE_UNAVAILABLE);
    }

    @Test
    void reportsTimeoutWhenPythonDoesNotRespondInTime() {
        server.enqueue(new MockResponse()
                .setHeader("Content-Type", "application/json")
                .setBodyDelay(2, TimeUnit.SECONDS)
                .setBody("{\"status\":\"ok\",\"service\":\"agent-runtime\"}"));

        AgentClient client = client(server.url("/").toString(), Duration.ofSeconds(1), Duration.ofMillis(200));

        assertThatThrownBy(client::health)
                .isInstanceOf(AgentServiceException.class)
                .hasMessage("Agent 服务响应超时")
                .extracting(exception -> ((AgentServiceException) exception).getStatus())
                .isEqualTo(HttpStatus.GATEWAY_TIMEOUT);
    }

    @Test
    void reportsBadGatewayWhenPythonReturnsErrorStatus() {
        server.enqueue(new MockResponse().setResponseCode(500).setBody("failed"));

        AgentClient client = client(server.url("/").toString(), Duration.ofSeconds(1), Duration.ofSeconds(1));

        assertThatThrownBy(client::health)
                .isInstanceOf(AgentServiceException.class)
                .hasMessage("Agent 服务返回异常状态")
                .extracting(exception -> ((AgentServiceException) exception).getStatus())
                .isEqualTo(HttpStatus.BAD_GATEWAY);
    }

    @Test
    void forwardsTheCurrentRequestIdWithoutGeneratingAnother() throws Exception {
        server.enqueue(new MockResponse()
                .setHeader("Content-Type", "application/json")
                .setBody("{\"status\":\"ok\",\"service\":\"agent-runtime\",\"requestId\":\"from-python\"}"));
        MDC.put(RequestIds.MDC_KEY, "trace-shared");
        try {
            client(server.url("/").toString(), Duration.ofSeconds(1), Duration.ofSeconds(1)).health();
            assertThat(server.takeRequest().getHeader(RequestIds.HEADER)).isEqualTo("trace-shared");
        } finally {
            MDC.remove(RequestIds.MDC_KEY);
        }
    }

    @Test
    void generatesASafeRequestIdWhenNoWebContextExists() throws Exception {
        server.enqueue(new MockResponse()
                .setHeader("Content-Type", "application/json")
                .setBody("{\"status\":\"ok\",\"service\":\"agent-runtime\"}"));
        MDC.clear();

        client(server.url("/").toString(), Duration.ofSeconds(1), Duration.ofSeconds(1)).health();

        String requestId = server.takeRequest().getHeader(RequestIds.HEADER);
        assertThat(requestId).matches("[A-Za-z0-9_-]{1,128}");
    }

    @Test
    void readsHistoryWithTrustedIdsAndWithoutThreadId() throws Exception {
        UUID tenantId = UUID.fromString("00000000-0000-4000-8000-00000000e291");
        UUID actorId = UUID.fromString("00000000-0000-4000-8000-00000000a291");
        UUID conversationId = UUID.fromString("00000000-0000-4000-8000-00000000c291");
        server.enqueue(new MockResponse()
                .setHeader("Content-Type", "application/json")
                .setBody("""
                        {"messages":[{"role":"assistant","content":"部门负责人审批","status":"answered","source_ids":["chunk-1"],"sources":[{"chunk_id":"chunk-1","content":"付款申请须先由部门负责人审批。"}],"rag_result":{"blob":"hidden"},"thread_id":"kq:hidden"}]}
                        """));
        MDC.put(RequestIds.MDC_KEY, "trace-history");
        try {
            var messages = client(server.url("/").toString(), Duration.ofSeconds(1), Duration.ofSeconds(1), "gateway-test-token")
                    .readKnowledgeQaHistory(tenantId, actorId, conversationId);
            assertThat(messages).hasSize(1);
            assertThat(messages.getFirst().content()).isEqualTo("部门负责人审批");
            assertThat(messages.getFirst().sourceIds()).containsExactly("chunk-1");
            assertThat(messages.getFirst().sources()).extracting(HistorySource::content)
                    .containsExactly("付款申请须先由部门负责人审批。");
        } finally {
            MDC.remove(RequestIds.MDC_KEY);
        }

        RecordedRequest request = server.takeRequest();
        assertThat(request.getMethod()).isEqualTo("GET");
        assertThat(request.getPath()).contains("/internal/v1/knowledge-qa/conversations/" + conversationId + "/messages");
        assertThat(request.getPath()).contains("tenant_id=" + tenantId);
        assertThat(request.getPath()).contains("actor_id=" + actorId);
        assertThat(request.getHeader("X-Agent-Token")).isEqualTo("gateway-test-token");
        assertThat(request.getHeader(RequestIds.HEADER)).isEqualTo("trace-history");
        assertThat(request.getPath()).doesNotContain("thread_id");
        assertThat(request.getHeader("Authorization")).isNull();
    }

    @Test
    void mapsHistoryFailuresToSafeMessages() {
        UUID tenantId = UUID.randomUUID();
        UUID actorId = UUID.randomUUID();
        UUID conversationId = UUID.randomUUID();
        server.enqueue(new MockResponse().setResponseCode(404).setBody("{\"detail\":\"thread_forbidden\",\"thread_id\":\"kq:secret\"}"));
        server.enqueue(new MockResponse().setResponseCode(409).setBody("thread_id blob"));
        server.enqueue(new MockResponse().setResponseCode(500).setBody("blob secret"));
        AgentClient client = client(server.url("/").toString(), Duration.ofSeconds(1), Duration.ofSeconds(1), "gateway-test-token");

        assertThatThrownBy(() -> client.readKnowledgeQaHistory(tenantId, actorId, conversationId))
                .isInstanceOf(AgentServiceException.class)
                .hasMessage("会话不存在");
        assertThatThrownBy(() -> client.readKnowledgeQaHistory(tenantId, actorId, conversationId))
                .isInstanceOf(AgentServiceException.class)
                .hasMessage("同一个会话正在执行，请等待本次完成后再继续。");
        assertThatThrownBy(() -> client.readKnowledgeQaHistory(tenantId, actorId, conversationId))
                .isInstanceOf(AgentServiceException.class)
                .hasMessage("Agent 服务返回异常状态")
                .hasMessageNotContaining("secret")
                .hasMessageNotContaining("thread_id");
    }

    @Test
    void refusesToCallPythonWhenTheInternalTokenIsMissing() {
        AgentClient client = streamClient(server.url("/").toString(), "");

        assertThatThrownBy(() -> client.writeKnowledgeQaStream(
                UUID.randomUUID(), UUID.randomUUID(), UUID.randomUUID(),
                "问题", null, null, "trace-missing-token", new MockHttpServletResponse()))
                .isInstanceOf(AgentServiceException.class)
                .hasMessage("Agent 服务认证未配置");
        assertThat(server.getRequestCount()).isZero();
    }

    @Test
    void sendsTheInternalCredentialAndRequestIdWithoutTheUserJwt() throws Exception {
        server.enqueue(new MockResponse()
                .setHeader("Content-Type", "text/event-stream")
                .setBody("event: message\ndata: {\"answer\":\"部门负责人审批\"}\n\n"));
        MDC.put(RequestIds.MDC_KEY, "trace-stream");
        UUID tenantId = UUID.fromString("00000000-0000-4000-8000-00000000e291");
        UUID actorId = UUID.fromString("00000000-0000-4000-8000-00000000a291");
        UUID conversationId = UUID.fromString("00000000-0000-4000-8000-00000000c291");
        MockHttpServletResponse response = new MockHttpServletResponse();
        try {
            streamClient(server.url("/").toString(), "gateway-test-token").writeKnowledgeQaStream(
                    tenantId, actorId, conversationId, "付款由谁审批？", 5, "vector", "trace-stream", response);
        } finally {
            MDC.remove(RequestIds.MDC_KEY);
        }

        RecordedRequest request = server.takeRequest();
        assertThat(request.getHeader("X-Agent-Token")).isEqualTo("gateway-test-token");
        assertThat(request.getHeader(RequestIds.HEADER)).isEqualTo("trace-stream");
        assertThat(request.getHeader("Authorization")).isNull();
        String body = request.getBody().readUtf8();
        assertThat(body).contains("\"tenant_id\":\"" + tenantId + "\"");
        assertThat(body).contains("\"actor_id\":\"" + actorId + "\"");
        assertThat(body).contains("\"conversation_id\":\"" + conversationId + "\"");
        assertThat(body).contains("付款由谁审批？");
        assertThat(body).contains("\"actor_ticket\":");
        assertThat(body).doesNotContain("thread_id");
        assertThat(request.getPath()).isEqualTo("/internal/v1/knowledge-qa/stream");
        String forwarded = text(response);
        assertThat(forwarded).contains("部门负责人审批");
        assertThat(response.getContentType()).isEqualTo("text/event-stream;charset=UTF-8");
        assertThat(response.getHeader("Cache-Control")).isEqualTo("no-cache, no-transform");
        assertThat(response.getHeader("X-Accel-Buffering")).isEqualTo("no");
        assertThat(response.getHeader(RequestIds.HEADER)).isEqualTo("trace-stream");
    }

    @Test
    void returnsASafeErrorWhenPythonIsDown() {
        ActorTicketStore tickets = new ActorTicketStore();
        AgentClient client = streamClient("http://127.0.0.1:1", "gateway-test-token", tickets);
        MockHttpServletResponse response = new MockHttpServletResponse();

        assertThatThrownBy(() -> client.writeKnowledgeQaStream(
                UUID.randomUUID(), UUID.randomUUID(), UUID.randomUUID(),
                "问题", null, null, "trace-down", response))
                .isInstanceOf(AgentServiceException.class)
                .hasMessage("Agent 服务不可用")
                .extracting(exception -> ((AgentServiceException) exception).getStatus())
                .isEqualTo(HttpStatus.SERVICE_UNAVAILABLE);
        assertThat(response.getContentAsByteArray()).isEmpty();
        assertThat(tickets.size()).isZero();
    }

    @Test
    void appendsRunFailedWhenPythonClosesAfterTheStreamStarts() throws Exception {
        ActorTicketStore tickets = new ActorTicketStore();
        try (ServerSocket socket = new ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))) {
            Thread serverThread = new Thread(() -> serveOneChunkThenClose(socket));
            serverThread.start();
            MockHttpServletResponse response = new MockHttpServletResponse();
            streamClient("http://127.0.0.1:" + socket.getLocalPort(), "gateway-test-token", tickets)
                    .writeKnowledgeQaStream(
                            UUID.randomUUID(), UUID.randomUUID(), UUID.randomUUID(),
                            "问题", null, null, "trace-cut", response);
            serverThread.join(5000);
            String forwarded = text(response);
            assertThat(forwarded).contains("event: run_started");
            assertThat(forwarded).contains("event: run_failed");
            assertThat(forwarded).contains("\"error_code\":\"agent_stream_interrupted\"");
            assertThat(forwarded).contains("Agent 服务响应中断");
            assertThat(forwarded).doesNotContain("Exception");
            assertThat(forwarded).doesNotContain("actor_ticket");
            assertThat(tickets.size()).isZero();
        }
    }

    @Test
    void clearsTheTicketWhenTheClientDisconnects() throws Exception {
        ActorTicketStore tickets = new ActorTicketStore();
        server.enqueue(new MockResponse()
                .setHeader("Content-Type", "text/event-stream")
                .setBody("event: run_started\ndata: {\"request_id\":\"trace-drop\"}\n\n"
                        + "event: message\ndata: {\"answer\":\"still going\"}\n\n"));
        MockHttpServletResponse response = new MockHttpServletResponse() {
            @Override
            public ServletOutputStream getOutputStream() {
                ServletOutputStream delegate = super.getOutputStream();
                return new ServletOutputStream() {
                    @Override
                    public boolean isReady() {
                        return true;
                    }

                    @Override
                    public void setWriteListener(WriteListener listener) {
                    }

                    @Override
                    public void write(int value) throws IOException {
                        if (isCommitted()) {
                            throw new IOException("client closed");
                        }
                        delegate.write(value);
                    }
                };
            }
        };

        streamClient(server.url("/").toString(), "gateway-test-token", tickets).writeKnowledgeQaStream(
                UUID.randomUUID(), UUID.randomUUID(), UUID.randomUUID(),
                "问题", null, null, "trace-drop", response);

        assertThat(tickets.size()).isZero();
        assertThat(text(response)).contains("event: run_started");
        assertThat(text(response)).doesNotContain("agent_stream_interrupted");
        assertThat(text(response)).doesNotContain("Exception");
    }

    @Test
    void hidesAnUpstreamAuthFailureBody() {
        server.enqueue(new MockResponse().setResponseCode(401).setBody("sk-live-secret"));
        MockHttpServletResponse response = new MockHttpServletResponse();

        assertThatThrownBy(() -> streamClient(server.url("/").toString(), "gateway-test-token").writeKnowledgeQaStream(
                UUID.randomUUID(), UUID.randomUUID(), UUID.randomUUID(),
                "问题", null, null, "trace-auth", response))
                .isInstanceOf(AgentServiceException.class)
                .hasMessage("Agent 服务认证失败")
                .satisfies(exception -> assertThat(exception.getMessage()).doesNotContain("sk-live-secret"));
        assertThat(response.getContentAsByteArray()).isEmpty();
    }

    @Test
    void copiesARunFailedEventWithoutWrappingIt() {
        server.enqueue(new MockResponse()
                .setHeader("Content-Type", "text/event-stream")
                .setBody("""
                        event: run_started
                        data: {"request_id":"trace-failed"}

                        event: run_failed
                        data: {"error_code":"rag_failed","safe_error_message":"RuntimeError","request_id":"trace-failed"}

                        """));
        MockHttpServletResponse response = new MockHttpServletResponse();

        streamClient(server.url("/").toString(), "gateway-test-token").writeKnowledgeQaStream(
                UUID.randomUUID(), UUID.randomUUID(), UUID.randomUUID(),
                "问题", null, null, "trace-failed", response);

        String forwarded = text(response);
        assertThat(forwarded.indexOf("event: run_started")).isLessThan(forwarded.indexOf("event: run_failed"));
        assertThat(forwarded).contains("\"error_code\":\"rag_failed\"");
        assertThat(forwarded).doesNotContain("\"code\"");
    }

    @Test
    void flushesEachSseChunkBeforeTheUpstreamCloses() throws Exception {
        CountDownLatch releaseSecond = new CountDownLatch(1);
        CountDownLatch firstEvent = new CountDownLatch(1);
        AtomicReference<String> rawRequest = new AtomicReference<>();
        try (ServerSocket socket = new ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))) {
            Thread serverThread = new Thread(() -> serveTwoChunks(socket, rawRequest, releaseSecond));
            serverThread.start();
            FlushWatchingResponse response = new FlushWatchingResponse(firstEvent);
            UUID tenantId = UUID.fromString("00000000-0000-4000-8000-00000000e293");
            UUID actorId = UUID.fromString("00000000-0000-4000-8000-00000000a293");
            UUID conversationId = UUID.fromString("00000000-0000-4000-8000-00000000c293");
            Thread caller = new Thread(() -> streamClient(
                    "http://127.0.0.1:" + socket.getLocalPort(), "gateway-test-token")
                    .writeKnowledgeQaStream(
                            tenantId, actorId, conversationId, "付款由谁审批？", null, null,
                            "trace-live", response));
            caller.start();
            assertThat(firstEvent.await(5, TimeUnit.SECONDS)).isTrue();
            String midway = text(response);
            assertThat(midway).contains("event: run_started");
            assertThat(midway).doesNotContain("event: message");
            releaseSecond.countDown();
            caller.join(5000);
            serverThread.join(5000);
            String forwarded = text(response);
            assertThat(forwarded.indexOf("event: run_started")).isLessThan(forwarded.indexOf("event: message"));
            assertThat(forwarded.indexOf("event: message")).isLessThan(forwarded.indexOf("event: run_completed"));
            assertThat(forwarded).contains("部门负责人");
            String request = rawRequest.get();
            assertThat(request).contains("X-Agent-Token: gateway-test-token");
            assertThat(request).contains("X-Request-ID: trace-live");
            assertThat(request).contains(tenantId.toString());
            assertThat(request).contains(actorId.toString());
            assertThat(request).doesNotContain("thread_id");
            assertThat(request.toLowerCase()).doesNotContain("authorization:");
        }
    }

    private static String text(MockHttpServletResponse response) {
        return new String(response.getContentAsByteArray(), StandardCharsets.UTF_8);
    }

    private AgentClient client(String baseUrl, Duration connectTimeout, Duration readTimeout) {
        return client(baseUrl, connectTimeout, readTimeout, "");
    }

    private AgentClient client(String baseUrl, Duration connectTimeout, Duration readTimeout, String token) {
        AgentServiceProperties properties = new AgentServiceProperties();
        properties.setBaseUrl(baseUrl);
        properties.setHealthPath("/api/v1/health");
        properties.setKnowledgeQaHistoryPath("/internal/v1/knowledge-qa/conversations/{conversationId}/messages");
        properties.setConnectTimeout(connectTimeout);
        properties.setReadTimeout(readTimeout);
        properties.setInternalToken(token);

        RestClient restClient = RestClientConfig.create(baseUrl, connectTimeout, readTimeout);
        return new AgentClient(restClient, restClient, properties, new ActorTicketStore());
    }

    private AgentClient streamClient(String baseUrl, String token) {
        return streamClient(baseUrl, token, new ActorTicketStore());
    }

    private AgentClient streamClient(String baseUrl, String token, ActorTicketStore tickets) {
        AgentServiceProperties properties = new AgentServiceProperties();
        properties.setBaseUrl(baseUrl);
        properties.setHealthPath("/api/v1/health");
        properties.setKnowledgeQaPath("/internal/v1/knowledge-qa/stream");
        properties.setConnectTimeout(Duration.ofSeconds(1));
        properties.setReadTimeout(Duration.ofSeconds(1));
        properties.setStreamReadTimeout(Duration.ofSeconds(5));
        properties.setInternalToken(token);
        RestClient health = RestClientConfig.create(baseUrl, Duration.ofSeconds(1), Duration.ofSeconds(1));
        RestClient stream = RestClientConfig.create(baseUrl, Duration.ofSeconds(1), Duration.ofSeconds(5));
        return new AgentClient(health, stream, properties, tickets);
    }

    private static void serveOneChunkThenClose(ServerSocket socket) {
        try (Socket client = socket.accept()) {
            client.setSoTimeout(5000);
            InputStream input = client.getInputStream();
            ByteArrayOutputStream header = new ByteArrayOutputStream();
            while (!header.toString(StandardCharsets.ISO_8859_1).contains("\r\n\r\n")) {
                int value = input.read();
                if (value < 0) {
                    return;
                }
                header.write(value);
            }
            readRequestBody(input, header.toString(StandardCharsets.ISO_8859_1));
            OutputStream output = client.getOutputStream();
            output.write(("HTTP/1.1 200 OK\r\n"
                    + "Content-Type: text/event-stream;charset=UTF-8\r\n"
                    + "Transfer-Encoding: chunked\r\n\r\n").getBytes(StandardCharsets.US_ASCII));
            writeChunk(output, "event: run_started\ndata: {\"request_id\":\"trace-cut\"}\n\n");
            output.flush();
        } catch (Exception ignored) {
            // Closing the socket is the upstream failure under test.
        }
    }

    private static void serveTwoChunks(
            ServerSocket socket,
            AtomicReference<String> rawRequest,
            CountDownLatch releaseSecond) {
        try (Socket client = socket.accept()) {
            client.setSoTimeout(5000);
            InputStream input = client.getInputStream();
            ByteArrayOutputStream header = new ByteArrayOutputStream();
            while (!header.toString(StandardCharsets.ISO_8859_1).contains("\r\n\r\n")) {
                int value = input.read();
                if (value < 0) {
                    return;
                }
                header.write(value);
            }
            String head = header.toString(StandardCharsets.ISO_8859_1);
            byte[] body = readRequestBody(input, head);
            rawRequest.set(head + new String(body, StandardCharsets.UTF_8));
            OutputStream output = client.getOutputStream();
            output.write(("HTTP/1.1 200 OK\r\n"
                    + "Content-Type: text/event-stream;charset=UTF-8\r\n"
                    + "Transfer-Encoding: chunked\r\n\r\n").getBytes(StandardCharsets.US_ASCII));
            writeChunk(output, "event: run_started\ndata: {\"request_id\":\"trace-live\"}\n\n");
            if (!releaseSecond.await(5, TimeUnit.SECONDS)) {
                return;
            }
            writeChunk(output, "event: message\ndata: {\"answer\":\"部门负责人\"}\n\n");
            writeChunk(output, "event: run_completed\ndata: {\"status\":\"answered\"}\n\n");
            output.write("0\r\n\r\n".getBytes(StandardCharsets.US_ASCII));
            output.flush();
        } catch (Exception exception) {
            rawRequest.compareAndSet(null, exception.getClass().getSimpleName());
        }
    }

    private static byte[] readRequestBody(InputStream input, String header) throws IOException {
        if (header.toLowerCase().contains("transfer-encoding: chunked")) {
            ByteArrayOutputStream body = new ByteArrayOutputStream();
            while (true) {
                int size = Integer.parseInt(readAsciiLine(input).trim(), 16);
                if (size == 0) {
                    readAsciiLine(input);
                    break;
                }
                body.write(input.readNBytes(size));
                readAsciiLine(input);
            }
            return body.toByteArray();
        }
        return input.readNBytes(contentLength(header));
    }

    private static String readAsciiLine(InputStream input) throws IOException {
        ByteArrayOutputStream line = new ByteArrayOutputStream();
        int previous = -1;
        while (true) {
            int value = input.read();
            if (value < 0) {
                break;
            }
            if (previous == '\r' && value == '\n') {
                break;
            }
            if (value != '\r') {
                line.write(value);
            }
            previous = value;
        }
        return line.toString(StandardCharsets.US_ASCII);
    }

    private static int contentLength(String header) {
        for (String line : header.split("\r\n")) {
            if (line.toLowerCase().startsWith("content-length:")) {
                return Integer.parseInt(line.substring(line.indexOf(':') + 1).trim());
            }
        }
        return 0;
    }

    private static void writeChunk(OutputStream output, String text) throws IOException {
        byte[] bytes = text.getBytes(StandardCharsets.UTF_8);
        output.write((Integer.toHexString(bytes.length) + "\r\n").getBytes(StandardCharsets.US_ASCII));
        output.write(bytes);
        output.write("\r\n".getBytes(StandardCharsets.US_ASCII));
        output.flush();
    }

    private static final class FlushWatchingResponse extends MockHttpServletResponse {
        private final CountDownLatch firstEvent;

        private FlushWatchingResponse(CountDownLatch firstEvent) {
            this.firstEvent = firstEvent;
        }

        @Override
        public void flushBuffer() {
            super.flushBuffer();
            if (new String(getContentAsByteArray(), StandardCharsets.UTF_8).contains("event: run_started")) {
                firstEvent.countDown();
            }
        }
    }
}
