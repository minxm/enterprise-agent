package com.example.enterprise.auth;

import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;

import javax.crypto.SecretKey;
import java.nio.charset.StandardCharsets;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.Date;
import java.util.Optional;
import java.util.UUID;

public class JwtService {

    static final String TENANT_CLAIM = "tenantId";

    private final SecretKey key;
    private final String issuer;
    private final Duration ttl;
    private final Clock clock;

    public JwtService(String secret, String issuer, Duration ttl, Clock clock) {
        if (secret == null || secret.getBytes(StandardCharsets.UTF_8).length < 32) {
            throw new IllegalStateException("JWT 签名密钥未配置或长度不足");
        }
        this.key = Keys.hmacShaKeyFor(secret.getBytes(StandardCharsets.UTF_8));
        this.issuer = issuer;
        this.ttl = ttl;
        this.clock = clock;
    }

    public String issue(UUID userId, UUID tenantId) {
        Instant issuedAt = clock.instant();
        return issue(userId, tenantId, issuedAt, issuedAt.plus(ttl));
    }

    public String issue(UUID userId, UUID tenantId, Instant issuedAt, Instant expiresAt) {
        return Jwts.builder()
                .subject(userId.toString())
                .claim(TENANT_CLAIM, tenantId.toString())
                .issuer(issuer)
                .issuedAt(Date.from(issuedAt))
                .expiration(Date.from(expiresAt))
                .signWith(key)
                .compact();
    }

    public Optional<AuthenticatedUser> parse(String token) {
        try {
            var claims = Jwts.parser()
                    .verifyWith(key)
                    .requireIssuer(issuer)
                    .clock(() -> Date.from(clock.instant()))
                    .build()
                    .parseSignedClaims(token)
                    .getPayload();
            UUID userId = UUID.fromString(claims.getSubject());
            UUID tenantId = UUID.fromString(claims.get(TENANT_CLAIM, String.class));
            return Optional.of(new AuthenticatedUser(userId, tenantId, null));
        } catch (RuntimeException exception) {
            return Optional.empty();
        }
    }
}
