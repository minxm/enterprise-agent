package com.example.enterprise.identity;

import java.util.UUID;

/**
 * 服务端可信租户。只允许认证过滤器或测试夹具写入，不从请求参数填充。
 */
public final class TenantContext {

    private static final ThreadLocal<UUID> CURRENT = new ThreadLocal<>();

    private TenantContext() {
    }

    public static void set(UUID tenantId) {
        if (tenantId == null) {
            throw new IdentityException(org.springframework.http.HttpStatus.BAD_REQUEST, "租户上下文未建立");
        }
        CURRENT.set(tenantId);
    }

    public static UUID require() {
        UUID tenantId = CURRENT.get();
        if (tenantId == null) {
            throw new IdentityException(org.springframework.http.HttpStatus.FORBIDDEN, "租户上下文未建立");
        }
        return tenantId;
    }

    public static void clear() {
        CURRENT.remove();
    }
}
