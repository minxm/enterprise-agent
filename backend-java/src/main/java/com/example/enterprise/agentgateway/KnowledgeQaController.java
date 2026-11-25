package com.example.enterprise.agentgateway;

import com.example.enterprise.auth.AuthenticatedUser;
import com.example.enterprise.common.request.RequestIds;
import com.example.enterprise.common.response.ApiResponse;
import com.example.enterprise.identity.IdentityException;
import com.example.enterprise.identity.IdentityService;
import com.example.enterprise.identity.TenantContext;
import com.example.enterprise.integration.agent.AgentClient;
import com.example.enterprise.integration.agent.AgentServiceException;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.Valid;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.UUID;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1/agent")
public class KnowledgeQaController {

    static final String READ_TENANT = "memory:read_tenant";

    private final AgentConversationService conversations;
    private final AgentClient agentClient;
    private final ObjectProvider<IdentityService> identities;

    public KnowledgeQaController(
            AgentConversationService conversations,
            AgentClient agentClient,
            ObjectProvider<IdentityService> identities) {
        this.conversations = conversations;
        this.agentClient = agentClient;
        this.identities = identities;
    }

    @GetMapping("/conversations")
    public ApiResponse<ConversationPage> listConversations(
            @RequestParam(defaultValue = "0") int page,
            @RequestParam(defaultValue = "20") int size) {
        AuthenticatedUser user = currentUser();
        UUID tenantId = TenantContext.require();
        return ApiResponse.success(conversations.list(tenantId, user.userId(), page, size));
    }

    @PostMapping("/conversations")
    public ApiResponse<ConversationCreated> createConversation() {
        AuthenticatedUser user = currentUser();
        UUID tenantId = TenantContext.require();
        UUID conversationId = conversations.create(tenantId, user.userId());
        return ApiResponse.success(new ConversationCreated(conversationId));
    }

    @GetMapping("/conversations/{conversationId}/messages")
    public ApiResponse<HistoryMessages> messages(@PathVariable UUID conversationId) {
        AuthenticatedUser user = currentUser();
        UUID tenantId = TenantContext.require();
        conversations.requireOwned(tenantId, user.userId(), conversationId);
        List<HistoryMessage> messages = agentClient.readKnowledgeQaHistory(
                tenantId, user.userId(), conversationId, allowTenantRead(user.userId()));
        return ApiResponse.success(new HistoryMessages(messages));
    }

    @PostMapping(value = "/knowledge-qa/stream", produces = "text/event-stream")
    public void stream(@Valid @RequestBody KnowledgeQaStreamRequest request, HttpServletResponse response) {
        AuthenticatedUser user = currentUser();
        UUID tenantId = TenantContext.require();
        conversations.requireOwned(tenantId, user.userId(), request.conversationId());
        String requestId = RequestIds.current().orElseGet(RequestIds::newId);
        try {
            agentClient.writeKnowledgeQaStream(
                    tenantId,
                    user.userId(),
                    request.conversationId(),
                    request.question().strip(),
                    request.topK(),
                    request.retrievalStrategy(),
                    allowTenantRead(user.userId()),
                    requestId,
                    response);
        } catch (AgentServiceException exception) {
            String message = exception.getMessage() == null ? "Agent 服务不可用" : exception.getMessage();
            writeJsonFailure(response, exception.getStatus().value(), message);
        }
    }

    /**
     * Writes the failure directly. The stream method declares text/event-stream,
     * so returning a JSON ResponseEntity is rejected by content negotiation and
     * becomes an empty 500 once the browser asks for an event stream.
     */
    private static void writeJsonFailure(HttpServletResponse response, int status, String message) {
        if (response.isCommitted()) {
            return;
        }
        response.reset();
        response.setStatus(status);
        response.setCharacterEncoding(StandardCharsets.UTF_8.name());
        response.setContentType(MediaType.APPLICATION_JSON_VALUE);
        String requestId = RequestIds.current().orElse(null);
        String json = "{\"code\":" + status
                + ",\"message\":" + quote(message)
                + ",\"data\":null,\"requestId\":" + (requestId == null ? "null" : quote(requestId))
                + "}";
        try {
            response.getOutputStream().write(json.getBytes(StandardCharsets.UTF_8));
            response.flushBuffer();
        } catch (IOException exception) {
            // The client went away before the error body could be written.
        }
    }

    private static String quote(String value) {
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

    private boolean allowTenantRead(UUID userId) {
        IdentityService identity = identities.getIfAvailable();
        if (identity == null) {
            return false;
        }
        return identity.hasPermission(userId, READ_TENANT);
    }

    private static AuthenticatedUser currentUser() {
        Authentication authentication = SecurityContextHolder.getContext().getAuthentication();
        if (authentication == null || !(authentication.getPrincipal() instanceof AuthenticatedUser user)) {
            throw new IdentityException(HttpStatus.UNAUTHORIZED, "未认证");
        }
        return user;
    }
}
