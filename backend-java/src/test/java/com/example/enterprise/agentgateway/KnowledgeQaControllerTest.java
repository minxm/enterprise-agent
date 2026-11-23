package com.example.enterprise.agentgateway;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyBoolean;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;
import static org.springframework.security.test.web.servlet.request.SecurityMockMvcRequestPostProcessors.authentication;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.example.enterprise.auth.AuthenticatedUser;
import com.example.enterprise.common.request.RequestIdFilter;
import com.example.enterprise.common.request.RequestIds;
import com.example.enterprise.common.response.GlobalExceptionHandler;
import com.example.enterprise.config.SecurityConfig;
import com.example.enterprise.identity.IdentityException;
import com.example.enterprise.identity.IdentityExceptionHandler;
import com.example.enterprise.identity.IdentityService;
import com.example.enterprise.integration.agent.AgentClient;
import com.example.enterprise.integration.agent.AgentServiceException;
import com.example.enterprise.internal.ActorTicketException;
import jakarta.servlet.http.HttpServletResponse;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.context.annotation.Import;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(KnowledgeQaController.class)
@Import({
        GlobalExceptionHandler.class,
        IdentityExceptionHandler.class,
        RequestIdFilter.class,
        SecurityConfig.class
})
class KnowledgeQaControllerTest {

    private static final UUID TENANT = UUID.fromString("00000000-0000-4000-8000-00000000e291");
    private static final UUID OTHER_TENANT = UUID.fromString("00000000-0000-4000-8000-00000000e292");
    private static final UUID USER = UUID.fromString("00000000-0000-4000-8000-00000000a291");
    private static final UUID CONVERSATION = UUID.fromString("00000000-0000-4000-8000-00000000c291");
    private static final UUID OLDER = UUID.fromString("00000000-0000-4000-8000-00000000c292");

    @Autowired
    private MockMvc mockMvc;

    @MockitoBean
    private AgentClient agentClient;

    @MockitoBean
    private AgentConversationService conversations;

    @MockitoBean
    private IdentityService identity;

    @Test
    void rejectsAnUnauthenticatedStream() throws Exception {
        mockMvc.perform(post("/api/v1/agent/knowledge-qa/stream")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body(CONVERSATION, 5, "vector")))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.message").value("未认证"));
        verifyNoInteractions(agentClient, conversations);
    }

    @Test
    void usesTheAuthenticatedTenantAndActor() throws Exception {
        mockMvc.perform(post("/api/v1/agent/knowledge-qa/stream")
                        .with(authentication(principal()))
                        .header(RequestIds.HEADER, "trace-qa")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {"conversationId":"%s","question":"付款由谁审批？","topK":5,"retrievalStrategy":"vector","tenantId":"%s","actorId":"%s","threadId":"client-picked"}
                                """.formatted(CONVERSATION, OTHER_TENANT, OTHER_TENANT)))
                .andExpect(status().isOk());

        verify(conversations).requireOwned(TENANT, USER, CONVERSATION);
        verify(agentClient).writeKnowledgeQaStream(
                eq(TENANT),
                eq(USER),
                eq(CONVERSATION),
                eq("付款由谁审批？"),
                eq(5),
                eq("vector"),
                eq(false),
                eq("trace-qa"),
                any(HttpServletResponse.class));
    }

    @Test
    void computesTenantMemoryReadOnTheServer() throws Exception {
        when(identity.hasPermission(USER, KnowledgeQaController.READ_TENANT)).thenReturn(true);

        mockMvc.perform(post("/api/v1/agent/knowledge-qa/stream")
                        .with(authentication(principal()))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {"conversationId":"%s","question":"付款由谁审批？","allowTenantRead":false,"allow_tenant_read":true,"tenantId":"%s"}
                                """.formatted(CONVERSATION, OTHER_TENANT)))
                .andExpect(status().isOk());

        verify(agentClient).writeKnowledgeQaStream(
                eq(TENANT),
                eq(USER),
                eq(CONVERSATION),
                eq("付款由谁审批？"),
                isNull(),
                isNull(),
                eq(true),
                any(),
                any(HttpServletResponse.class));
    }

    @Test
    void rejectsAConversationTheCallerDoesNotOwn() throws Exception {
        doThrow(new IdentityException(HttpStatus.NOT_FOUND, "会话不存在"))
                .when(conversations).requireOwned(TENANT, USER, CONVERSATION);

        mockMvc.perform(post("/api/v1/agent/knowledge-qa/stream")
                        .with(authentication(principal()))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body(CONVERSATION, 5, "vector")))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").value("会话不存在"));
        verify(agentClient, never()).writeKnowledgeQaStream(
                any(), any(), any(), any(), any(), any(), anyBoolean(), any(), any());
    }

    @Test
    void rejectsAnIllegalTopKAndStrategy() throws Exception {
        mockMvc.perform(post("/api/v1/agent/knowledge-qa/stream")
                        .with(authentication(principal()))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body(CONVERSATION, 21, "vector")))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.message").value("请求参数不合法"));
        mockMvc.perform(post("/api/v1/agent/knowledge-qa/stream")
                        .with(authentication(principal()))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body(CONVERSATION, 5, "keyword")))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.message").value("请求参数不合法"));
        verifyNoInteractions(agentClient, conversations);
    }

    @Test
    void returnsASafeErrorWhenPythonIsUnavailable() throws Exception {
        doThrow(new AgentServiceException(HttpStatus.SERVICE_UNAVAILABLE, "Agent 服务不可用"))
                .when(agentClient).writeKnowledgeQaStream(any(), any(), any(), any(), any(), any(), anyBoolean(), any(), any());

        mockMvc.perform(post("/api/v1/agent/knowledge-qa/stream")
                        .with(authentication(principal()))
                        .header(RequestIds.HEADER, "trace-down")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body(CONVERSATION, null, null)))
                .andExpect(status().isServiceUnavailable())
                .andExpect(jsonPath("$.message").value("Agent 服务不可用"))
                .andExpect(jsonPath("$.requestId").value("trace-down"));
    }

    @Test
    void returnsJsonInsteadOfAnEmptyBodyWhenTheBrowserAcceptsOnlySse() throws Exception {
        doThrow(new AgentServiceException(HttpStatus.SERVICE_UNAVAILABLE, "Agent 服务不可用"))
                .when(agentClient).writeKnowledgeQaStream(any(), any(), any(), any(), any(), any(), anyBoolean(), any(), any());

        mockMvc.perform(post("/api/v1/agent/knowledge-qa/stream")
                        .with(authentication(principal()))
                        .header(RequestIds.HEADER, "trace-sse-down")
                        .accept(MediaType.TEXT_EVENT_STREAM)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body(CONVERSATION, null, null)))
                .andExpect(status().isServiceUnavailable())
                .andExpect(content().contentTypeCompatibleWith(MediaType.APPLICATION_JSON))
                .andExpect(jsonPath("$.code").value(503))
                .andExpect(jsonPath("$.message").value("Agent 服务不可用"))
                .andExpect(jsonPath("$.requestId").value("trace-sse-down"))
                .andExpect(content().string(org.hamcrest.Matchers.not(org.hamcrest.Matchers.containsString("Exception"))))
                .andExpect(content().string(org.hamcrest.Matchers.not(org.hamcrest.Matchers.containsString("actor_ticket"))));
    }

    @Test
    void aCommittedStreamDoesNotReceiveASecondJsonBody() throws Exception {
        doAnswer(invocation -> {
            HttpServletResponse response = invocation.getArgument(8);
            response.setContentType("text/event-stream");
            response.getOutputStream().write("event: run_started\ndata: {}\n\n".getBytes(StandardCharsets.UTF_8));
            response.flushBuffer();
            throw new ActorTicketException();
        }).when(agentClient).writeKnowledgeQaStream(any(), any(), any(), any(), any(), any(), anyBoolean(), any(), any());

        mockMvc.perform(post("/api/v1/agent/knowledge-qa/stream")
                        .with(authentication(principal()))
                        .accept(MediaType.TEXT_EVENT_STREAM)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body(CONVERSATION, null, null)))
                .andExpect(status().isOk())
                .andExpect(content().string(org.hamcrest.Matchers.containsString("event: run_started")))
                .andExpect(content().string(org.hamcrest.Matchers.not(org.hamcrest.Matchers.containsString("Ticket 存储不可用"))));
    }

    @Test
    void returnsBadGatewayJsonWhenPythonReturnsAnUnexpectedStatus() throws Exception {
        doThrow(new AgentServiceException(HttpStatus.BAD_GATEWAY, "Agent 服务返回异常状态"))
                .when(agentClient).writeKnowledgeQaStream(any(), any(), any(), any(), any(), any(), anyBoolean(), any(), any());

        mockMvc.perform(post("/api/v1/agent/knowledge-qa/stream")
                        .with(authentication(principal()))
                        .accept(MediaType.TEXT_EVENT_STREAM)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body(CONVERSATION, null, null)))
                .andExpect(status().isBadGateway())
                .andExpect(jsonPath("$.message").value("Agent 服务返回异常状态"))
                .andExpect(content().string(org.hamcrest.Matchers.not(org.hamcrest.Matchers.containsString("sk-"))))
                .andExpect(content().string(org.hamcrest.Matchers.not(org.hamcrest.Matchers.containsString("actor_ticket"))));
    }

    @Test
    void forwardsARunFailedEventAsSse() throws Exception {
        doAnswer(invocation -> {
            HttpServletResponse response = invocation.getArgument(8);
            response.setStatus(200);
            response.setContentType("text/event-stream;charset=UTF-8");
            response.getOutputStream().write(
                    "event: run_failed\ndata: {\"error_code\":\"rag_failed\",\"request_id\":\"trace-failed\"}\n\n"
                            .getBytes(StandardCharsets.UTF_8));
            return null;
        }).when(agentClient).writeKnowledgeQaStream(
                any(), any(), any(), any(), isNull(), isNull(), anyBoolean(), eq("trace-failed"), any());

        mockMvc.perform(post("/api/v1/agent/knowledge-qa/stream")
                        .with(authentication(principal()))
                        .header(RequestIds.HEADER, "trace-failed")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body(CONVERSATION, null, null)))
                .andExpect(status().isOk())
                .andExpect(content().contentTypeCompatibleWith("text/event-stream"))
                .andExpect(content().string(org.hamcrest.Matchers.containsString("event: run_failed")))
                .andExpect(content().string(org.hamcrest.Matchers.not(org.hamcrest.Matchers.containsString("\"code\""))));
    }

    @Test
    void createsAConversationForTheAuthenticatedUser() throws Exception {
        when(conversations.create(TENANT, USER)).thenReturn(CONVERSATION);

        mockMvc.perform(post("/api/v1/agent/conversations").with(authentication(principal())))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.conversationId").value(CONVERSATION.toString()));
        verify(conversations).create(TENANT, USER);
    }

    @Test
    void rejectsAnUnauthenticatedConversationList() throws Exception {
        mockMvc.perform(get("/api/v1/agent/conversations"))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.message").value("未认证"));
        verifyNoInteractions(agentClient, conversations);
    }

    @Test
    void listsConversationsForTheAuthenticatedOwnerNewestFirst() throws Exception {
        when(conversations.list(TENANT, USER, 0, 20)).thenReturn(new ConversationPage(List.of(
                new ConversationSummary(CONVERSATION, Instant.parse("2026-01-02T00:00:00Z")),
                new ConversationSummary(OLDER, Instant.parse("2026-01-01T00:00:00Z"))), 0, 20, 2));

        mockMvc.perform(get("/api/v1/agent/conversations")
                        .with(authentication(principal()))
                        .queryParam("tenantId", OTHER_TENANT.toString())
                        .queryParam("ownerUserId", OTHER_TENANT.toString())
                        .queryParam("thread_id", "client-picked"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.total").value(2))
                .andExpect(jsonPath("$.data.page").value(0))
                .andExpect(jsonPath("$.data.size").value(20))
                .andExpect(jsonPath("$.data.items[0].conversationId").value(CONVERSATION.toString()))
                .andExpect(jsonPath("$.data.items[0].createdAt").value("2026-01-02T00:00:00Z"))
                .andExpect(jsonPath("$.data.items[1].conversationId").value(OLDER.toString()))
                .andExpect(jsonPath("$.data.items[0].thread_id").doesNotExist())
                .andExpect(jsonPath("$.data.ownerUserId").doesNotExist());

        verify(conversations).list(TENANT, USER, 0, 20);
    }

    @Test
    void forwardsTheRequestedConversationPage() throws Exception {
        when(conversations.list(TENANT, USER, 1, 1)).thenReturn(new ConversationPage(List.of(
                new ConversationSummary(OLDER, Instant.parse("2026-01-01T00:00:00Z"))), 1, 1, 2));

        mockMvc.perform(get("/api/v1/agent/conversations")
                        .with(authentication(principal()))
                        .queryParam("page", "1")
                        .queryParam("size", "1"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.items[0].conversationId").value(OLDER.toString()))
                .andExpect(jsonPath("$.data.total").value(2));

        verify(conversations).list(TENANT, USER, 1, 1);
    }

    @Test
    void rejectsAnIllegalConversationPage() throws Exception {
        when(conversations.list(TENANT, USER, -1, 20))
                .thenThrow(new IdentityException(HttpStatus.BAD_REQUEST, "请求参数不合法"));

        mockMvc.perform(get("/api/v1/agent/conversations")
                        .with(authentication(principal()))
                        .queryParam("page", "-1"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.message").value("请求参数不合法"));
    }

    @Test
    void rejectsAnUnauthenticatedHistoryRead() throws Exception {
        mockMvc.perform(get("/api/v1/agent/conversations/" + CONVERSATION + "/messages"))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.message").value("未认证"));
        verifyNoInteractions(agentClient, conversations);
    }

    @Test
    void readsHistoryForTheAuthenticatedOwner() throws Exception {
        when(agentClient.readKnowledgeQaHistory(TENANT, USER, CONVERSATION)).thenReturn(List.of(
                new HistoryMessage("user", "第一题", "answered", List.of(), List.of()),
                new HistoryMessage("assistant", "部门负责人审批", "answered", List.of("chunk-1"),
                        List.of(new HistorySource("chunk-1", "付款申请须先由部门负责人审批。")))));

        mockMvc.perform(get("/api/v1/agent/conversations/" + CONVERSATION + "/messages")
                        .with(authentication(principal()))
                        .queryParam("tenantId", OTHER_TENANT.toString())
                        .queryParam("actorId", OTHER_TENANT.toString())
                        .queryParam("thread_id", "client-picked"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.messages[0].role").value("user"))
                .andExpect(jsonPath("$.data.messages[1].content").value("部门负责人审批"))
                .andExpect(jsonPath("$.data.messages[1].sourceIds[0]").value("chunk-1"))
                .andExpect(jsonPath("$.data.messages[1].sources[0].content").value("付款申请须先由部门负责人审批。"))
                .andExpect(jsonPath("$.data.messages[1].thread_id").doesNotExist())
                .andExpect(jsonPath("$.data.thread_id").doesNotExist());

        verify(conversations).requireOwned(TENANT, USER, CONVERSATION);
        verify(agentClient).readKnowledgeQaHistory(TENANT, USER, CONVERSATION);
    }

    @Test
    void hidesHistoryWhenAnotherUserOrTenantDoesNotOwnTheConversation() throws Exception {
        doThrow(new IdentityException(HttpStatus.NOT_FOUND, "会话不存在"))
                .when(conversations).requireOwned(TENANT, USER, CONVERSATION);

        mockMvc.perform(get("/api/v1/agent/conversations/" + CONVERSATION + "/messages")
                        .with(authentication(principal())))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").value("会话不存在"));
        verify(agentClient, never()).readKnowledgeQaHistory(any(), any(), any());
    }

    @Test
    void returnsASafeErrorWhenHistoryPythonIsUnavailable() throws Exception {
        doThrow(new AgentServiceException(HttpStatus.BAD_GATEWAY, "Agent 服务返回异常状态"))
                .when(agentClient).readKnowledgeQaHistory(TENANT, USER, CONVERSATION);

        mockMvc.perform(get("/api/v1/agent/conversations/" + CONVERSATION + "/messages")
                        .with(authentication(principal())))
                .andExpect(status().isBadGateway())
                .andExpect(jsonPath("$.message").value("Agent 服务返回异常状态"))
                .andExpect(jsonPath("$.message").value(org.hamcrest.Matchers.not(org.hamcrest.Matchers.containsString("thread_id"))));
    }

    private static UsernamePasswordAuthenticationToken principal() {
        return new UsernamePasswordAuthenticationToken(
                new AuthenticatedUser(USER, TENANT, "ada"), null, List.of());
    }

    private static String body(UUID conversationId, Integer topK, String strategy) {
        String topKJson = topK == null ? "" : ",\"topK\":" + topK;
        String strategyJson = strategy == null ? "" : ",\"retrievalStrategy\":\"" + strategy + "\"";
        return "{\"conversationId\":\"" + conversationId + "\",\"question\":\"付款由谁审批？\"" + topKJson + strategyJson + "}";
    }
}
