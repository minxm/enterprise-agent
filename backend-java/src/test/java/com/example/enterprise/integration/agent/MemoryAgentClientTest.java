package com.example.enterprise.integration.agent;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.example.enterprise.config.AgentServiceProperties;
import com.example.enterprise.config.RestClientConfig;
import java.time.Duration;
import java.time.Instant;
import java.util.UUID;
import okhttp3.mockwebserver.MockResponse;
import okhttp3.mockwebserver.MockWebServer;
import okhttp3.mockwebserver.RecordedRequest;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;
import org.springframework.web.client.RestClient;

class MemoryAgentClientTest {

    private static final String TOKEN = "memory-internal-token-day39";
    private static final UUID TENANT = UUID.fromString("00000000-0000-4000-8000-00000000e291");
    private static final UUID ACTOR = UUID.fromString("00000000-0000-4000-8000-00000000a291");
    private static final UUID MEMORY = UUID.fromString("00000000-0000-4000-8000-00000000d291");
    private static final String SECRET_BODY = "以后优先使用表格 sk-live postgres://agent_app:secret@db";

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
    void sendsTheTokenOnlyAsAHeaderAndKeepsServerGrants() throws Exception {
        server.enqueue(json(200, view("user", "以后优先使用表格")));

        var created = client().create(
                TENANT, ACTOR, false, true, "user", "preference", "以后优先使用表格", null, null);

        RecordedRequest request = server.takeRequest();
        assertThat(created.content()).isEqualTo("以后优先使用表格");
        assertThat(created.memoryType()).isEqualTo("preference");
        assertThat(request.getPath()).isEqualTo("/internal/v1/memories");
        assertThat(request.getHeader("X-Agent-Token")).isEqualTo(TOKEN);
        assertThat(request.getBody().readUtf8())
                .contains("\"allow_tenant_read\":false")
                .contains("\"allow_tenant_write\":true")
                .contains(TENANT.toString())
                .contains(ACTOR.toString())
                .doesNotContain(TOKEN)
                .doesNotContain("\"source\"")
                .doesNotContain("\"confidence\"");
    }

    @Test
    void mapsPythonStatusesWithoutReturningTheBodyOrToken() {
        enqueueError(409);
        enqueueError(404);
        enqueueError(403);
        enqueueError(422);
        enqueueError(401);
        enqueueError(503);
        MemoryAgentClient python = client();

        assertSafe(HttpStatus.CONFLICT, "记忆版本冲突",
                () -> python.update(TENANT, ACTOR, false, false, MEMORY, 1, "新口径", null));
        assertSafe(HttpStatus.NOT_FOUND, "记忆不存在",
                () -> python.get(TENANT, ACTOR, true, false, MEMORY));
        assertSafe(HttpStatus.FORBIDDEN, "无权访问租户记忆",
                () -> python.archive(TENANT, ACTOR, false, false, MEMORY, 1));
        assertSafe(HttpStatus.BAD_REQUEST, "请求参数不合法",
                () -> python.delete(TENANT, ACTOR, false, false, MEMORY, 1));
        assertSafe(HttpStatus.BAD_GATEWAY, "Agent 服务认证失败",
                () -> python.create(TENANT, ACTOR, false, false, "user", "preference", "以后优先使用表格", null, null));
        assertSafe(HttpStatus.SERVICE_UNAVAILABLE, "Agent 服务不可用",
                () -> python.list(TENANT, ACTOR, false, false, "user", null, null, false));
    }

    @Test
    void reportsUnavailableWhenPythonCannotBeReached() {
        MemoryAgentClient python = client("http://127.0.0.1:1");

        assertThatThrownBy(() -> python.list(TENANT, ACTOR, false, false, null, null, null, false))
                .isInstanceOf(AgentServiceException.class)
                .hasMessage("Agent 服务不可用")
                .extracting(exception -> ((AgentServiceException) exception).getStatus())
                .isEqualTo(HttpStatus.SERVICE_UNAVAILABLE);
    }

    private void assertSafe(HttpStatus status, String message, Runnable action) {
        assertThatThrownBy(action::run)
                .isInstanceOf(AgentServiceException.class)
                .hasMessage(message)
                .satisfies(error -> {
                    assertThat(error.getMessage()).doesNotContain(TOKEN);
                    assertThat(error.getMessage()).doesNotContain("sk-live");
                    assertThat(error.getMessage()).doesNotContain("secret");
                    assertThat(error.getMessage()).doesNotContain("以后优先使用表格");
                    assertThat(((AgentServiceException) error).getStatus()).isEqualTo(status);
                });
    }

    private void enqueueError(int status) {
        server.enqueue(new MockResponse()
                .setResponseCode(status)
                .setHeader("Content-Type", "application/json")
                .setBody("{\"message\":\"" + SECRET_BODY + " " + TOKEN + "\",\"trace\":\"traceback\"}"));
    }

    private static MockResponse json(int status, String body) {
        return new MockResponse()
                .setResponseCode(status)
                .setHeader("Content-Type", "application/json")
                .setBody(body);
    }

    private static String view(String scope, String content) {
        return """
                {"id":"%s","scope":"%s","memory_type":"preference","content":"%s","source":"user_explicit","source_conversation_id":null,"status":"active","created_at":"%s","updated_at":"%s","expires_at":null,"deleted_at":null,"version":1,"tenant_id":"%s","confidence":"1.00"}
                """.formatted(MEMORY, scope, content, Instant.parse("2026-01-02T00:00:00Z"), Instant.parse("2026-01-02T00:00:00Z"), TENANT);
    }

    private MemoryAgentClient client() {
        return client(server.url("/").toString());
    }

    private MemoryAgentClient client(String baseUrl) {
        AgentServiceProperties properties = new AgentServiceProperties();
        properties.setBaseUrl(baseUrl);
        properties.setHealthPath("/api/v1/health");
        properties.setConnectTimeout(Duration.ofMillis(300));
        properties.setReadTimeout(Duration.ofSeconds(1));
        properties.setInternalToken(TOKEN);
        RestClient restClient = RestClientConfig.create(baseUrl, Duration.ofMillis(300), Duration.ofSeconds(1));
        return new MemoryAgentClient(restClient, properties);
    }
}
