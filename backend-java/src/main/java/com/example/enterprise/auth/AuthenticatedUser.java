package com.example.enterprise.auth;

import java.util.UUID;

public record AuthenticatedUser(UUID userId, UUID tenantId, String loginName) {
}
