package com.example.enterprise.agentgateway;

import com.example.enterprise.auth.AuthenticatedUser;
import com.example.enterprise.common.request.RequestIds;
import com.example.enterprise.common.response.ApiResponse;
import com.example.enterprise.identity.IdentityException;
import com.example.enterprise.identity.TenantContext;
import com.example.enterprise.integration.agent.AgentClient;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.Valid;
import java.util.List;
import java.util.UUID;
import org.springframework.http.HttpStatus;
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

    private final AgentConversationService conversations;
    private final AgentClient agentClient;

    public KnowledgeQaController(AgentConversationService conversations, AgentClient agentClient) {
        this.conversations = conversations;
        this.agentClient = agentClient;
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
        List<HistoryMessage> messages = agentClient.readKnowledgeQaHistory(tenantId, user.userId(), conversationId);
        return ApiResponse.success(new HistoryMessages(messages));
    }

    @PostMapping(value = "/knowledge-qa/stream", produces = "text/event-stream")
    public void stream(@Valid @RequestBody KnowledgeQaStreamRequest request, HttpServletResponse response) {
        AuthenticatedUser user = currentUser();
        UUID tenantId = TenantContext.require();
        conversations.requireOwned(tenantId, user.userId(), request.conversationId());
        String requestId = RequestIds.current().orElseGet(RequestIds::newId);
        agentClient.writeKnowledgeQaStream(
                tenantId,
                user.userId(),
                request.conversationId(),
                request.question().strip(),
                request.topK(),
                request.retrievalStrategy(),
                requestId,
                response);
    }

    private static AuthenticatedUser currentUser() {
        Authentication authentication = SecurityContextHolder.getContext().getAuthentication();
        if (authentication == null || !(authentication.getPrincipal() instanceof AuthenticatedUser user)) {
            throw new IdentityException(HttpStatus.UNAUTHORIZED, "未认证");
        }
        return user;
    }
}
