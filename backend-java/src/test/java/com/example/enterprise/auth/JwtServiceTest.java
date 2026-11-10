package com.example.enterprise.auth;

import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class JwtServiceTest {

    private static final String SECRET = "0123456789abcdef0123456789abcdef";
    private static final Clock CLOCK = Clock.fixed(Instant.parse("2026-11-10T08:00:00Z"), ZoneOffset.UTC);

    private final JwtService tokens = new JwtService(SECRET, "enterprise-agent", Duration.ofHours(2), CLOCK);

    @Test
    void validTokenCarriesUserAndTenant() {
        UUID userId = UUID.randomUUID();
        UUID tenantId = UUID.randomUUID();

        AuthenticatedUser parsed = tokens.parse(tokens.issue(userId, tenantId)).orElseThrow();

        assertEquals(userId, parsed.userId());
        assertEquals(tenantId, parsed.tenantId());
    }

    @Test
    void expiredTokenIsRejected() {
        String token = tokens.issue(
                UUID.randomUUID(),
                UUID.randomUUID(),
                CLOCK.instant().minus(Duration.ofHours(3)),
                CLOCK.instant().minus(Duration.ofHours(1)));

        assertTrue(tokens.parse(token).isEmpty());
    }

    @Test
    void wrongIssuerIsRejected() {
        JwtService other = new JwtService(SECRET, "other-issuer", Duration.ofHours(2), CLOCK);

        assertTrue(tokens.parse(other.issue(UUID.randomUUID(), UUID.randomUUID())).isEmpty());
    }

    @Test
    void wrongSignatureIsRejected() {
        JwtService other = new JwtService(
                "fedcba9876543210fedcba9876543210",
                "enterprise-agent",
                Duration.ofHours(2),
                CLOCK);

        assertTrue(tokens.parse(other.issue(UUID.randomUUID(), UUID.randomUUID())).isEmpty());
    }

    @Test
    void malformedTokenIsRejected() {
        assertTrue(tokens.parse("not-a-jwt").isEmpty());
        assertTrue(tokens.parse("a.b.c").isEmpty());
        assertTrue(tokens.parse("").isEmpty());
    }
}
