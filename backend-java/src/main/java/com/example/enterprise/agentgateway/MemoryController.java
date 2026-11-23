package com.example.enterprise.agentgateway;

import com.example.enterprise.auth.AuthenticatedUser;
import com.example.enterprise.common.response.ApiResponse;
import com.example.enterprise.identity.IdentityException;
import com.example.enterprise.identity.IdentityService;
import com.example.enterprise.identity.TenantContext;
import com.example.enterprise.integration.agent.MemoryAgentClient;
import com.fasterxml.jackson.databind.JsonNode;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Validator;
import java.util.Set;
import java.util.UUID;
import org.springframework.context.annotation.Profile;
import org.springframework.http.HttpStatus;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@Profile({"dev", "prod"})
@RequestMapping("/api/v1/agent/memories")
public class MemoryController {

    static final String READ_TENANT = "memory:read_tenant";
    static final String MANAGE_TENANT = "memory:manage_tenant";

    private static final Set<String> LIST_PARAMS = Set.of("scope", "memoryType", "status", "includeExpired");

    private final IdentityService identity;
    private final AgentConversationService conversations;
    private final MemoryAgentClient memories;
    private final Validator validator;

    public MemoryController(
            IdentityService identity,
            AgentConversationService conversations,
            MemoryAgentClient memories,
            Validator validator) {
        this.identity = identity;
        this.conversations = conversations;
        this.memories = memories;
        this.validator = validator;
    }

    @PostMapping
    public ApiResponse<MemoryView> create(@RequestBody JsonNode body) {
        CreateMemoryRequest request = StrictJson.read(body, CreateMemoryRequest.class, validator);
        AuthenticatedUser user = currentUser();
        UUID tenantId = TenantContext.require();
        Grants grants = grants(user.userId());
        if ("tenant".equals(request.scope()) && !grants.write()) {
            throw new IdentityException(HttpStatus.FORBIDDEN, "无权管理租户记忆");
        }
        if (request.sourceConversationId() != null) {
            conversations.requireOwned(tenantId, user.userId(), request.sourceConversationId());
        }
        String content = request.content().strip();
        if (content.isEmpty()) {
            throw new IdentityException(HttpStatus.BAD_REQUEST, "请求参数不合法");
        }
        return ApiResponse.success(memories.create(
                tenantId,
                user.userId(),
                grants.read(),
                grants.write(),
                request.scope(),
                request.memoryType(),
                content,
                request.expiresAt(),
                request.sourceConversationId()));
    }

    @GetMapping
    public ApiResponse<MemoryPage> list(
            HttpServletRequest request,
            @RequestParam(required = false) String scope,
            @RequestParam(required = false) String memoryType,
            @RequestParam(required = false) String status,
            @RequestParam(defaultValue = "false") boolean includeExpired) {
        rejectUnknown(request, LIST_PARAMS);
        AuthenticatedUser user = currentUser();
        UUID tenantId = TenantContext.require();
        Grants grants = grants(user.userId());
        if ("tenant".equals(scope) && !grants.read()) {
            throw new IdentityException(HttpStatus.FORBIDDEN, "无权读取租户记忆");
        }
        String effectiveScope = scope;
        if (effectiveScope == null && !grants.read()) {
            effectiveScope = "user";
        }
        return ApiResponse.success(memories.list(
                tenantId,
                user.userId(),
                grants.read(),
                grants.write(),
                effectiveScope,
                memoryType,
                status,
                includeExpired));
    }

    @GetMapping("/{memoryId}")
    public ApiResponse<MemoryView> get(@PathVariable UUID memoryId, HttpServletRequest request) {
        rejectUnknown(request, Set.of());
        AuthenticatedUser user = currentUser();
        UUID tenantId = TenantContext.require();
        Grants grants = grants(user.userId());
        return ApiResponse.success(memories.get(
                tenantId, user.userId(), grants.read(), grants.write(), memoryId));
    }

    @PutMapping("/{memoryId}")
    public ApiResponse<MemoryView> update(
            @PathVariable UUID memoryId,
            @RequestBody JsonNode body) {
        UpdateMemoryRequest request = StrictJson.read(body, UpdateMemoryRequest.class, validator);
        AuthenticatedUser user = currentUser();
        UUID tenantId = TenantContext.require();
        Grants grants = grants(user.userId());
        String content = request.content().strip();
        if (content.isEmpty()) {
            throw new IdentityException(HttpStatus.BAD_REQUEST, "请求参数不合法");
        }
        return ApiResponse.success(memories.update(
                tenantId,
                user.userId(),
                grants.read(),
                grants.write(),
                memoryId,
                request.expectedVersion(),
                content,
                request.expiresAt()));
    }

    @PostMapping("/{memoryId}/archive")
    public ApiResponse<MemoryView> archive(
            @PathVariable UUID memoryId,
            @RequestBody JsonNode body) {
        MemoryVersionRequest request = StrictJson.read(body, MemoryVersionRequest.class, validator);
        AuthenticatedUser user = currentUser();
        UUID tenantId = TenantContext.require();
        Grants grants = grants(user.userId());
        return ApiResponse.success(memories.archive(
                tenantId,
                user.userId(),
                grants.read(),
                grants.write(),
                memoryId,
                request.expectedVersion()));
    }

    @DeleteMapping("/{memoryId}")
    public ApiResponse<MemoryView> delete(
            @PathVariable UUID memoryId,
            @RequestBody JsonNode body) {
        MemoryVersionRequest request = StrictJson.read(body, MemoryVersionRequest.class, validator);
        AuthenticatedUser user = currentUser();
        UUID tenantId = TenantContext.require();
        Grants grants = grants(user.userId());
        return ApiResponse.success(memories.delete(
                tenantId,
                user.userId(),
                grants.read(),
                grants.write(),
                memoryId,
                request.expectedVersion()));
    }

    private Grants grants(UUID userId) {
        return new Grants(
                identity.hasPermission(userId, READ_TENANT),
                identity.hasPermission(userId, MANAGE_TENANT));
    }

    private static void rejectUnknown(HttpServletRequest request, Set<String> allowed) {
        for (String name : request.getParameterMap().keySet()) {
            if (!allowed.contains(name)) {
                throw new IdentityException(HttpStatus.BAD_REQUEST, "请求参数不合法");
            }
        }
    }

    private static AuthenticatedUser currentUser() {
        Authentication authentication = SecurityContextHolder.getContext().getAuthentication();
        if (authentication == null || !(authentication.getPrincipal() instanceof AuthenticatedUser user)) {
            throw new IdentityException(HttpStatus.UNAUTHORIZED, "未认证");
        }
        return user;
    }

    private record Grants(boolean read, boolean write) {
    }
}
