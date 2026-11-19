package com.example.enterprise.agentgateway;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

/**
 * Calls the already running Java process, which forwards to the already running Python process.
 * It is skipped unless AGENT_LIVE_SSE=true. MockWebServer is not used.
 */
@EnabledIfEnvironmentVariable(named = "AGENT_LIVE_SSE", matches = "true")
class KnowledgeQaLiveStreamTest {

    private final ObjectMapper json = new ObjectMapper();
    private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).build();

    @Test
    void javaForwardsARealKnowledgeQaStreamAndReleasesTheThreadAfterDisconnect() throws Exception {
        String baseUrl = env("JAVA_BASE_URL", "http://127.0.0.1:8080");
        String token = login(baseUrl, env("LIVE_TENANT_CODE", ""), env("LIVE_USERNAME", ""), env("LIVE_PASSWORD", ""));
        String conversationId = createConversation(baseUrl, token);

        List<SseEvent> first = readEvents(postStream(baseUrl, token, conversationId, "付款申请的第一步由谁审批？", "live-day30-1"));
        System.out.println("FIRST_EVENTS " + names(first));
        System.out.println("FIRST_ANSWER " + messageOf(first));
        assertTrue(names(first).contains("run_started"), "missing run_started");
        assertTrue(names(first).contains("message"), "missing message");
        assertEquals("run_completed", names(first).getLast());
        assertFalse(messageOf(first).isBlank(), "first answer was empty");

        List<SseEvent> second = readEvents(postStream(baseUrl, token, conversationId, "合同最终由谁盖章？", "live-day30-2"));
        System.out.println("SECOND_EVENTS " + names(second));
        System.out.println("SECOND_ANSWER " + messageOf(second));
        assertEquals("run_completed", names(second).getLast());
        assertFalse(messageOf(second).isBlank());

        HttpResponse<InputStream> hanging = postStream(baseUrl, token, conversationId, "法务在合同审核中负责什么？", "live-day30-cancel");
        try (InputStream body = hanging.body()) {
            assertEquals(200, hanging.statusCode());
            assertTrue(readUntil(body, "run_started"), "upstream did not flush run_started before disconnect");
        }
        boolean released = false;
        List<SseEvent> followUp = List.of();
        for (int attempt = 0; attempt < 12; attempt++) {
            followUp = readEvents(postStream(baseUrl, token, conversationId, "合同金额超过多少必须由总经理审批？", "live-day30-after-" + attempt));
            String code = followUp.isEmpty() ? "" : followUp.getLast().data().path("error_code").asText("");
            if (!"thread_busy".equals(code)) {
                released = "run_completed".equals(names(followUp).getLast());
                break;
            }
            Thread.sleep(5000);
        }
        assertTrue(released, "thread stayed busy after the client disconnected");
    }

    private String login(String baseUrl, String tenant, String username, String password) throws Exception {
        assertFalse(tenant.isBlank(), "LIVE_TENANT_CODE 未设置");
        assertFalse(username.isBlank(), "LIVE_USERNAME 未设置");
        assertFalse(password.isBlank(), "LIVE_PASSWORD 未设置");
        String body = json.writeValueAsString(json.createObjectNode()
                .put("tenantCode", tenant)
                .put("username", username)
                .put("password", password));
        HttpResponse<String> response = http.send(
                HttpRequest.newBuilder(URI.create(baseUrl + "/api/auth/login"))
                        .header("Content-Type", "application/json")
                        .timeout(Duration.ofSeconds(10))
                        .POST(HttpRequest.BodyPublishers.ofString(body))
                        .build(),
                HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        assertEquals(200, response.statusCode(), "登录失败");
        String token = json.readTree(response.body()).path("data").path("token").asText("");
        assertFalse(token.isBlank(), "登录没有返回 token");
        return token;
    }

    private String createConversation(String baseUrl, String token) throws Exception {
        HttpResponse<String> response = http.send(authorized(baseUrl + "/api/v1/agent/conversations", token)
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString("{}"))
                .build(), HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        assertEquals(200, response.statusCode(), response.body());
        String conversationId = json.readTree(response.body()).path("data").path("conversationId").asText("");
        assertFalse(conversationId.isBlank());
        return conversationId;
    }

    private HttpResponse<InputStream> postStream(
            String baseUrl, String token, String conversationId, String question, String requestId) throws Exception {
        String body = json.writeValueAsString(json.createObjectNode()
                .put("conversationId", conversationId)
                .put("question", question)
                .put("retrievalStrategy", "vector"));
        HttpRequest request = authorized(baseUrl + "/api/v1/agent/knowledge-qa/stream", token)
                .header("Content-Type", "application/json")
                .header("Accept", "text/event-stream")
                .header("X-Request-ID", requestId)
                .timeout(Duration.ofMinutes(3))
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build();
        return http.send(request, HttpResponse.BodyHandlers.ofInputStream());
    }

    private HttpRequest.Builder authorized(String url, String token) {
        return HttpRequest.newBuilder(URI.create(url)).header("Authorization", "Bearer " + token);
    }

    private List<SseEvent> readEvents(HttpResponse<InputStream> response) throws Exception {
        assertEquals(200, response.statusCode());
        try (InputStream body = response.body()) {
            return parse(body);
        }
    }

    private boolean readUntil(InputStream body, String eventName) throws Exception {
        for (SseEvent event : parse(body, eventName)) {
            if (eventName.equals(event.event())) {
                return true;
            }
        }
        return false;
    }

    private List<SseEvent> parse(InputStream body) throws Exception {
        return parse(body, null);
    }

    private List<SseEvent> parse(InputStream body, String stopAfter) throws Exception {
        List<SseEvent> events = new ArrayList<>();
        BufferedReader reader = new BufferedReader(new InputStreamReader(body, StandardCharsets.UTF_8));
        String eventName = "message";
        StringBuilder data = new StringBuilder();
        String line;
        while ((line = reader.readLine()) != null) {
            if (line.isEmpty()) {
                if (!data.isEmpty()) {
                    SseEvent event = new SseEvent(eventName, json.readTree(data.toString()));
                    events.add(event);
                    if (stopAfter != null && stopAfter.equals(eventName)) {
                        return events;
                    }
                }
                eventName = "message";
                data.setLength(0);
                continue;
            }
            if (line.startsWith(":")) {
                continue;
            }
            if (line.startsWith("event:")) {
                eventName = line.substring(6).trim();
            } else if (line.startsWith("data:")) {
                data.append(line.substring(5).trim());
            }
        }
        return events;
    }

    private static List<String> names(List<SseEvent> events) {
        return events.stream().map(SseEvent::event).toList();
    }

    private static String messageOf(List<SseEvent> events) {
        return events.stream()
                .filter(event -> "message".equals(event.event()))
                .map(event -> event.data().path("answer").asText(""))
                .reduce((first, second) -> second)
                .orElse("");
    }

    private static String env(String name, String fallback) {
        String value = System.getenv(name);
        return value == null ? fallback : value;
    }

    private record SseEvent(String event, JsonNode data) {
    }
}
