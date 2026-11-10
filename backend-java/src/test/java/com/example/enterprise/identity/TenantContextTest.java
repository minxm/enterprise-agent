package com.example.enterprise.identity;

import com.example.enterprise.auth.AuthenticatedUser;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.context.SecurityContextHolder;

import java.util.List;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class TenantContextTest {

    @AfterEach
    void cleanup() {
        SecurityContextHolder.clearContext();
        TenantContext.clear();
    }

    @Test
    void tenantIdComesFromAuthenticatedUser() {
        UUID tenantId = UUID.randomUUID();
        authenticate(UUID.randomUUID(), tenantId);

        assertEquals(tenantId, TenantContext.require());
    }

    @Test
    void unauthenticatedRequestHasNoTrustedTenant() {
        IdentityException exception = assertThrows(IdentityException.class, TenantContext::require);

        assertEquals("租户上下文未建立", exception.getMessage());
        assertTrue(TenantContext.getTenantId().isEmpty());
    }

    @Test
    void clientSuppliedTenantDoesNotOverrideAuthentication() {
        UUID trusted = UUID.randomUUID();
        authenticate(UUID.randomUUID(), trusted);
        TenantContext.set(UUID.randomUUID());

        assertEquals(trusted, TenantContext.require());
    }

    @Test
    void clearRemovesTheThreadLocalTenant() {
        TenantContext.set(UUID.randomUUID());
        TenantContext.clear();

        assertTrue(TenantContext.getTenantId().isEmpty());
        assertThrows(IdentityException.class, TenantContext::require);
    }

    @Test
    void consecutiveTenantsDoNotLeakAcrossClear() {
        UUID first = UUID.randomUUID();
        UUID second = UUID.randomUUID();

        TenantContext.set(first);
        assertEquals(first, TenantContext.require());
        TenantContext.clear();

        TenantContext.set(second);
        assertEquals(second, TenantContext.require());
        assertTrue(TenantContext.getTenantId().isPresent());
    }

    private static void authenticate(UUID userId, UUID tenantId) {
        var authentication = UsernamePasswordAuthenticationToken.authenticated(
                new AuthenticatedUser(userId, tenantId, "ada"),
                null,
                List.of());
        SecurityContextHolder.getContext().setAuthentication(authentication);
    }
}
