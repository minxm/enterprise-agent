package com.example.enterprise.identity;

import java.util.UUID;

public final class IdentityViews {

    private IdentityViews() {
    }

    public record TenantView(UUID id, String code, String name, String status) {
    }

    public record UserView(UUID id, String loginName, String displayName, String status) {
    }

    public record RoleView(UUID id, String code, String name) {
    }

    public record PermissionView(UUID id, String code, String name) {
    }

    public record AssociationCheck(boolean valid, String reason) {
    }

    public record UserRoleCheck(UUID userId, UUID roleId) {
    }
}
