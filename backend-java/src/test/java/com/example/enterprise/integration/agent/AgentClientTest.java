package com.example.enterprise.integration.agent;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.example.enterprise.config.AgentServiceProperties;
import com.example.enterprise.config.RestClientConfig;
import java.time.Duration;
import java.util.concurrent.TimeUnit;
import okhttp3.mockwebserver.MockResponse;
import okhttp3.mockwebserver.MockWebServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;
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

    private AgentClient client(String baseUrl, Duration connectTimeout, Duration readTimeout) {
        AgentServiceProperties properties = new AgentServiceProperties();
        properties.setBaseUrl(baseUrl);
        properties.setHealthPath("/api/v1/health");
        properties.setConnectTimeout(connectTimeout);
        properties.setReadTimeout(readTimeout);

        RestClient restClient = RestClientConfig.create(baseUrl, connectTimeout, readTimeout);
        return new AgentClient(restClient, properties);
    }
}
