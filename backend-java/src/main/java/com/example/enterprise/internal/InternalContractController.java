package com.example.enterprise.internal;

import com.example.enterprise.auth.AuthenticatedUser;
import com.example.enterprise.common.response.ApiResponse;
import com.example.enterprise.config.AgentServiceProperties;
import com.example.enterprise.contract.ContractException;
import com.example.enterprise.contract.ContractService;
import com.example.enterprise.contract.ContractViews.AgentContractSnapshot;
import com.example.enterprise.identity.TenantContext;
import com.example.enterprise.internal.ActorTicketStore.Grant;
import jakarta.servlet.http.HttpServletRequest;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Set;
import org.springframework.context.annotation.Profile;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * Read-only contract lookup for the Python agent. Identity comes from the
 * ticket Java issued for the signed-in user. Tenant and actor values on the
 * request are rejected.
 */
@RestController
@Profile("dev")
@RequestMapping("/internal/v1/contracts")
public class InternalContractController {

    static final String TOKEN_HEADER = "X-Agent-Token";
    static final String TICKET_HEADER = "X-Actor-Ticket";

    private static final Set<String> ALLOWED_PARAMS = Set.of("contractNo");
    private static final Set<String> REJECTED_IDENTITY = Set.of(
            "tenant_id", "tenantId", "actor_id", "actorId", "user_id", "userId");

    private final ContractService contracts;
    private final ActorTicketStore tickets;
    private final AgentServiceProperties properties;

    public InternalContractController(
            ContractService contracts, ActorTicketStore tickets, AgentServiceProperties properties) {
        this.contracts = contracts;
        this.tickets = tickets;
        this.properties = properties;
    }

    @GetMapping
    public ApiResponse<AgentContractSnapshot> query(
            @RequestHeader(value = TOKEN_HEADER, required = false) String token,
            @RequestHeader(value = TICKET_HEADER, required = false) String ticket,
            @RequestParam(required = false) String contractNo,
            HttpServletRequest request) {
        if (!allowedQuery(request)) {
            throw new ContractException(HttpStatus.BAD_REQUEST, "请求参数不合法");
        }
        if (!tokenMatches(token)) {
            throw new ContractException(HttpStatus.UNAUTHORIZED, "未认证");
        }
        Grant grant = tickets.consume(ticket)
                .orElseThrow(() -> new ContractException(HttpStatus.UNAUTHORIZED, "未认证"));
        TenantContext.set(grant.tenantId());
        AuthenticatedUser user = new AuthenticatedUser(grant.actorId(), grant.tenantId(), "");
        return ApiResponse.success(contracts.queryForAgent(user, contractNo));
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

    private static boolean allowedQuery(HttpServletRequest request) {
        for (String name : request.getParameterMap().keySet()) {
            if (REJECTED_IDENTITY.contains(name) || !ALLOWED_PARAMS.contains(name)) {
                return false;
            }
        }
        return true;
    }
}
