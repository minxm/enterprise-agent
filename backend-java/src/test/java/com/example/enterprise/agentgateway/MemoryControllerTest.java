package com.example.enterprise.agentgateway;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyBoolean;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.ArgumentMatchers.nullable;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;
import static org.springframework.security.test.web.servlet.request.SecurityMockMvcRequestPostProcessors.authentication;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.example.enterprise.auth.AuthenticatedUser;
import com.example.enterprise.common.request.RequestIdFilter;
import com.example.enterprise.common.response.GlobalExceptionHandler;
import com.example.enterprise.config.SecurityConfig;
import com.example.enterprise.identity.IdentityException;
import com.example.enterprise.identity.IdentityExceptionHandler;
import com.example.enterprise.identity.IdentityService;
import com.example.enterprise.integration.agent.AgentServiceException;
import com.example.enterprise.integration.agent.MemoryAgentClient;
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
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(MemoryController.class)
@ActiveProfiles("dev")
@Import({
        GlobalExceptionHandler.class,
        IdentityExceptionHandler.class,
        RequestIdFilter.class,
        SecurityConfig.class
})
class MemoryControllerTest {

    private static final UUID TENANT = UUID.fromString("00000000-0000-4000-8000-00000000e291");
    private static final UUID OTHER_TENANT = UUID.fromString("00000000-0000-4000-8000-00000000e292");
    private static final UUID USER = UUID.fromString("00000000-0000-4000-8000-00000000a291");
    private static final UUID OTHER_USER = UUID.fromString("00000000-0000-4000-8000-00000000a292");
    private static final UUID MEMORY = UUID.fromString("00000000-0000-4000-8000-00000000d291");
    private static final UUID CONVERSATION = UUID.fromString("00000000-0000-4000-8000-00000000c291");
    private static final String TOKEN = "memory-internal-token-day39";

    @Autowired
    private MockMvc mockMvc;

    @MockitoBean
    private IdentityService identity;

    @MockitoBean
    private AgentConversationService conversations;

    @MockitoBean
    private MemoryAgentClient memories;

    @Test
    void rejectsAnUnauthenticatedRequest() throws Exception {
        mockMvc.perform(post("/api/v1/agent/memories")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(userBody()))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.message").value("未认证"));
        verifyNoInteractions(memories, conversations, identity);
    }

    @Test
    void ordinaryUserCreatesAndListsOnlyTheirOwnScope() throws Exception {
        when(memories.create(eq(TENANT), eq(USER), eq(false), eq(false), eq("user"), eq("preference"),
                eq("以后优先使用表格"), isNull(), isNull())).thenReturn(view("user", "以后优先使用表格"));
        when(memories.list(eq(TENANT), eq(USER), eq(false), eq(false), eq("user"), isNull(), isNull(), eq(false)))
                .thenReturn(new MemoryPage(List.of(view("user", "以后优先使用表格"))));

        mockMvc.perform(post("/api/v1/agent/memories")
                        .with(authentication(principal()))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {"scope":"user","memoryType":"preference","content":" 以后优先使用表格 "}
                                """))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.scope").value("user"))
                .andExpect(jsonPath("$.data.tenantId").doesNotExist())
                .andExpect(jsonPath("$.data.confidence").doesNotExist());

        mockMvc.perform(get("/api/v1/agent/memories").with(authentication(principal())))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.items[0].content").value("以后优先使用表格"));

        verify(memories).create(eq(TENANT), eq(USER), eq(false), eq(false), eq("user"), eq("preference"),
                eq("以后优先使用表格"), isNull(), isNull());
        verify(memories).list(TENANT, USER, false, false, "user", null, null, false);
    }

    @Test
    void ordinaryUserCannotUseTenantScope() throws Exception {
        mockMvc.perform(post("/api/v1/agent/memories")
                        .with(authentication(principal()))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(tenantBody()))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.message").value("无权管理租户记忆"));
        mockMvc.perform(get("/api/v1/agent/memories")
                        .with(authentication(principal()))
                        .queryParam("scope", "tenant"))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.message").value("无权读取租户记忆"));
        verify(memories, never()).create(any(), any(), anyBoolean(), anyBoolean(), any(), any(), any(), any(), any());
        verify(memories, never()).list(any(), any(), anyBoolean(), anyBoolean(), any(), any(), any(), anyBoolean());
    }

    @Test
    void tenantAdminReadsAndUpdatesTenantScope() throws Exception {
        grant(true, true);
        when(memories.list(TENANT, USER, true, true, "tenant", null, null, false))
                .thenReturn(new MemoryPage(List.of(view("tenant", "租户口径"))));
        when(memories.update(eq(TENANT), eq(USER), eq(true), eq(true), eq(MEMORY), eq(1), eq("新口径"), isNull()))
                .thenReturn(view("tenant", "新口径"));

        mockMvc.perform(get("/api/v1/agent/memories")
                        .with(authentication(principal()))
                        .queryParam("scope", "tenant"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.items[0].scope").value("tenant"));
        mockMvc.perform(put("/api/v1/agent/memories/" + MEMORY)
                        .with(authentication(principal()))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {"expectedVersion":1,"content":"新口径"}
                                """))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.content").value("新口径"));

        verify(identity, times(2)).hasPermission(USER, MemoryController.READ_TENANT);
        verify(identity, times(2)).hasPermission(USER, MemoryController.MANAGE_TENANT);
        verify(memories).list(TENANT, USER, true, true, "tenant", null, null, false);
        verify(memories).update(TENANT, USER, true, true, MEMORY, 1, "新口径", null);
    }

    @Test
    void readAndWriteAreCalculatedSeparately() throws Exception {
        grant(true, false);
        when(memories.list(eq(TENANT), eq(USER), eq(true), eq(false), isNull(), isNull(), isNull(), eq(false)))
                .thenReturn(new MemoryPage(List.of()));

        mockMvc.perform(get("/api/v1/agent/memories").with(authentication(principal())))
                .andExpect(status().isOk());
        mockMvc.perform(post("/api/v1/agent/memories")
                        .with(authentication(principal()))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(tenantBody()))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.message").value("无权管理租户记忆"));

        verify(memories).list(TENANT, USER, true, false, null, null, null, false);
        verify(memories, never()).create(any(), any(), anyBoolean(), anyBoolean(), any(), any(), any(), any(), any());
    }

    @Test
    void forgedIdentityGrantsAndProvenanceAreRejected() throws Exception {
        mockMvc.perform(post("/api/v1/agent/memories")
                        .with(authentication(principal()))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(userBody().replace("}", ",\"tenantId\":\"" + OTHER_TENANT + "\"}")))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.message").value("请求参数不合法"));
        mockMvc.perform(post("/api/v1/agent/memories")
                        .with(authentication(principal()))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(userBody().replace("}", ",\"userId\":\"" + OTHER_USER + "\"}")))
                .andExpect(status().isBadRequest());
        mockMvc.perform(post("/api/v1/agent/memories")
                        .with(authentication(principal()))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(userBody().replace("}", ",\"allowTenantRead\":true}")))
                .andExpect(status().isBadRequest());
        mockMvc.perform(post("/api/v1/agent/memories")
                        .with(authentication(principal()))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(userBody().replace("}", ",\"allowTenantWrite\":true}")))
                .andExpect(status().isBadRequest());
        mockMvc.perform(post("/api/v1/agent/memories")
                        .with(authentication(principal()))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(userBody().replace("}", ",\"source\":\"model_inferred\",\"confidence\":0.2}")))
                .andExpect(status().isBadRequest());
        mockMvc.perform(get("/api/v1/agent/memories")
                        .with(authentication(principal()))
                        .queryParam("tenantId", OTHER_TENANT.toString())
                        .queryParam("allowTenantWrite", "true"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.message").value("请求参数不合法"));
        verifyNoInteractions(memories);
    }

    @Test
    void sourceConversationMustBelongToTheCaller() throws Exception {
        org.mockito.Mockito.doThrow(new IdentityException(HttpStatus.NOT_FOUND, "会话不存在"))
                .when(conversations).requireOwned(TENANT, USER, CONVERSATION);

        mockMvc.perform(post("/api/v1/agent/memories")
                        .with(authentication(principal()))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(userBody().replace(
                                "}",
                                ",\"sourceConversationId\":\"" + CONVERSATION + "\"}")))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").value("会话不存在"));
        verify(memories, never()).create(any(), any(), anyBoolean(), anyBoolean(), any(), any(), any(), any(), any());
    }

    @Test
    void pythonFailuresStaySafe() throws Exception {
        when(memories.create(
                any(), any(), anyBoolean(), anyBoolean(), any(), any(), any(),
                nullable(Instant.class), nullable(UUID.class)))
                .thenThrow(new AgentServiceException(HttpStatus.SERVICE_UNAVAILABLE, "Agent 服务不可用"));
        mockMvc.perform(post("/api/v1/agent/memories")
                        .with(authentication(principal()))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(userBody()))
                .andExpect(status().isServiceUnavailable())
                .andExpect(jsonPath("$.message").value("Agent 服务不可用"))
                .andExpect(jsonPath("$.message").value(org.hamcrest.Matchers.not(
                        org.hamcrest.Matchers.containsString(TOKEN))));

        when(memories.update(
                any(), any(), anyBoolean(), anyBoolean(), any(), any(Integer.class), any(), nullable(Instant.class)))
                .thenThrow(new AgentServiceException(HttpStatus.CONFLICT, "记忆版本冲突"));
        mockMvc.perform(put("/api/v1/agent/memories/" + MEMORY)
                        .with(authentication(principal()))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {"expectedVersion":1,"content":"新口径"}
                                """))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.message").value("记忆版本冲突"));

        when(memories.get(TENANT, USER, false, false, MEMORY))
                .thenThrow(new AgentServiceException(HttpStatus.NOT_FOUND, "记忆不存在"));
        mockMvc.perform(get("/api/v1/agent/memories/" + MEMORY).with(authentication(principal())))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").value("记忆不存在"))
                .andExpect(jsonPath("$.message").value(org.hamcrest.Matchers.not(
                        org.hamcrest.Matchers.containsString(TOKEN))));

        mockMvc.perform(delete("/api/v1/agent/memories/" + MEMORY)
                        .with(authentication(principal()))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {"expectedVersion":1,"internalToken":"%s"}
                                """.formatted(TOKEN)))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.message").value("请求参数不合法"));
    }

    private void grant(boolean read, boolean write) {
        when(identity.hasPermission(USER, MemoryController.READ_TENANT)).thenReturn(read);
        when(identity.hasPermission(USER, MemoryController.MANAGE_TENANT)).thenReturn(write);
    }

    private static UsernamePasswordAuthenticationToken principal() {
        return new UsernamePasswordAuthenticationToken(
                new AuthenticatedUser(USER, TENANT, "ada"), null, List.of());
    }

    private static String userBody() {
        return """
                {"scope":"user","memoryType":"preference","content":"以后优先使用表格"}
                """;
    }

    private static String tenantBody() {
        return """
                {"scope":"tenant","memoryType":"organization_practice","content":"租户口径"}
                """;
    }

    private static MemoryView view(String scope, String content) {
        Instant at = Instant.parse("2026-01-02T00:00:00Z");
        return new MemoryView(MEMORY, scope, "preference", content, "user_explicit", null, "active", at, at, null, null, 1);
    }
}
