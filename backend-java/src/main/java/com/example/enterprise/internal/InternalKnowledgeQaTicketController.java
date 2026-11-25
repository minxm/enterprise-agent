package com.example.enterprise.internal;

import com.example.enterprise.common.response.ApiResponse;
import com.example.enterprise.config.AgentServiceProperties;
import com.example.enterprise.integration.agent.AgentServiceException;
import com.example.enterprise.internal.KnowledgeQaTicketStore.Claim;
import jakarta.servlet.http.HttpServletRequest;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.UUID;
import org.springframework.context.annotation.Profile;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * Redeems a Knowledge QA ticket. The caller cannot supply tenant, actor, or
 * permission values. Identity comes only from the stored claim.
 */
@RestController
@Profile({"dev", "prod"})
@RequestMapping("/internal/v1/knowledge-qa/tickets")
public class InternalKnowledgeQaTicketController {

    public static final String TOKEN_HEADER = "X-Agent-Token";
    public static final String TICKET_HEADER = "X-Knowledge-Qa-Ticket";

    private final KnowledgeQaTicketStore tickets;
    private final AgentServiceProperties properties;

    public InternalKnowledgeQaTicketController(KnowledgeQaTicketStore tickets, AgentServiceProperties properties) {
        this.tickets = tickets;
        this.properties = properties;
    }

    @GetMapping("/redeem")
    public ApiResponse<KnowledgeQaTicketView> redeem(
            @RequestHeader(value = TOKEN_HEADER, required = false) String token,
            @RequestHeader(value = TICKET_HEADER, required = false) String ticket,
            HttpServletRequest request) {
        if (!request.getParameterMap().isEmpty()) {
            throw new AgentServiceException(HttpStatus.BAD_REQUEST, "请求参数不合法");
        }
        if (!tokenMatches(token) || ticket == null || ticket.isBlank()) {
            throw new AgentServiceException(HttpStatus.UNAUTHORIZED, "未认证");
        }
        Claim claim = tickets.consume(ticket)
                .orElseThrow(() -> new AgentServiceException(HttpStatus.UNAUTHORIZED, "未认证"));
        return ApiResponse.success(KnowledgeQaTicketView.from(claim));
    }

    private boolean tokenMatches(String provided) {
        String expected = properties.getInternalToken() == null ? "" : properties.getInternalToken();
        byte[] expectedBytes = expected.getBytes(StandardCharsets.UTF_8);
        byte[] providedBytes = (provided == null ? "" : provided).getBytes(StandardCharsets.UTF_8);
        if (expectedBytes.length == 0 || expectedBytes.length != providedBytes.length) {
            return false;
        }
        return MessageDigest.isEqual(expectedBytes, providedBytes);
    }

    public record KnowledgeQaTicketView(
            UUID tenantId,
            UUID actorId,
            UUID conversationId,
            String action,
            boolean allowTenantRead,
            Instant expiresAt) {

        static KnowledgeQaTicketView from(Claim claim) {
            return new KnowledgeQaTicketView(
                    claim.tenantId(),
                    claim.actorId(),
                    claim.conversationId(),
                    claim.action(),
                    claim.allowTenantRead(),
                    claim.expiresAt());
        }
    }
}
