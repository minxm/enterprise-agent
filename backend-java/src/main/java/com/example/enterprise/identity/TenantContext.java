package com.example.enterprise.identity;

import com.example.enterprise.auth.AuthenticatedUser;
import org.springframework.http.HttpStatus;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;

import java.util.Optional;
import java.util.UUID;

/**
 * 可信租户来自已验证的 Authentication。测试夹具才可以直接 set。
 * 不读取 URL、Body 或普通请求头里的 tenantId。
 */
public final class TenantContext {

    private static final ThreadLocal<UUID> CURRENT = new ThreadLocal<>();

    private TenantContext() {
    }

    public static void set(UUID tenantId) {
        if (tenantId == null) {
            throw new IdentityException(HttpStatus.BAD_REQUEST, "租户上下文未建立");
        }
        CURRENT.set(tenantId);
    }

    public static Optional<UUID> getTenantId() {
        Authentication authentication = SecurityContextHolder.getContext().getAuthentication();
        if (authentication != null && authentication.getPrincipal() instanceof AuthenticatedUser user) {
            return Optional.of(user.tenantId());
        }
        return Optional.ofNullable(CURRENT.get());
    }

    public static UUID require() {
        return getTenantId().orElseThrow(() -> new IdentityException(HttpStatus.FORBIDDEN, "租户上下文未建立"));
    }

    public static void clear() {
        CURRENT.remove();
    }
}
