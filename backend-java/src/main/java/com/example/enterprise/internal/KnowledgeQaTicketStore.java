package com.example.enterprise.internal;

import com.example.enterprise.internal.ActorTicketStore.TicketCommands;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.SecureRandom;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.Base64;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.dao.DataAccessException;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

/**
 * One-time grant for a Knowledge QA stream or history read.
 *
 * The ticket is a random value. Redis stores only its SHA-256 and the identity
 * Java already verified. The key prefix is separate from contract actor tickets.
 */
@Component
public class KnowledgeQaTicketStore {

    public static final String ACTION_STREAM = "knowledge_qa_stream";
    public static final String ACTION_HISTORY = "knowledge_qa_history";
    public static final Duration TTL = Duration.ofSeconds(180);
    static final String KEY_PREFIX = "knowledge-qa-ticket:v1:";

    private static final Set<String> ACTIONS = Set.of(ACTION_STREAM, ACTION_HISTORY);
    private static final int ISSUE_ATTEMPTS = 3;
    private static final SecureRandom RANDOM = new SecureRandom();
    private static final Logger log = LoggerFactory.getLogger(KnowledgeQaTicketStore.class);

    public record Claim(
            UUID tenantId,
            UUID actorId,
            UUID conversationId,
            String action,
            boolean allowTenantRead,
            Instant expiresAt) {
    }

    private final TicketCommands commands;
    private final Duration ttl;
    private final String keyPrefix;
    private final Clock clock;

    @Autowired
    public KnowledgeQaTicketStore(ObjectProvider<StringRedisTemplate> redis) {
        this(commandsFor(redis), TTL, KEY_PREFIX, Clock.systemUTC());
    }

    KnowledgeQaTicketStore(TicketCommands commands, Duration ttl, String keyPrefix, Clock clock) {
        if (ttl == null || ttl.isZero() || ttl.isNegative()) {
            throw new IllegalArgumentException("ticket ttl must be positive");
        }
        if (keyPrefix == null || keyPrefix.isBlank()) {
            throw new IllegalArgumentException("ticket key prefix is required");
        }
        this.commands = commands;
        this.ttl = ttl;
        this.keyPrefix = keyPrefix;
        this.clock = clock == null ? Clock.systemUTC() : clock;
    }

    public String issue(
            UUID tenantId,
            UUID actorId,
            UUID conversationId,
            String action,
            boolean allowTenantRead) {
        if (tenantId == null || actorId == null || conversationId == null || !ACTIONS.contains(action)) {
            throw new IllegalArgumentException("knowledge qa ticket claim is incomplete");
        }
        TicketCommands store = ready();
        Instant expiresAt = clock.instant().plus(ttl);
        String value = encode(tenantId, actorId, conversationId, action, allowTenantRead, expiresAt);
        for (int attempt = 0; attempt < ISSUE_ATTEMPTS; attempt++) {
            String ticket = newTicket();
            try {
                if (store.setIfAbsent(storageKey(ticket), value, ttl)) {
                    return ticket;
                }
            } catch (DataAccessException exception) {
                throw unavailable(exception);
            }
        }
        throw unavailable("set-nx-rejected");
    }

    public Optional<Claim> consume(String ticket) {
        if (ticket == null || ticket.isBlank()) {
            return Optional.empty();
        }
        String stored;
        try {
            stored = ready().getAndDelete(storageKey(ticket));
        } catch (DataAccessException exception) {
            throw unavailable(exception);
        }
        return parse(stored, clock.instant());
    }

    public void revoke(String ticket) {
        if (ticket == null || ticket.isBlank()) {
            return;
        }
        try {
            ready().delete(storageKey(ticket));
        } catch (DataAccessException exception) {
            throw unavailable(exception);
        }
    }

    String storageKey(String ticket) {
        return keyPrefix + sha256(ticket);
    }

    static String encode(
            UUID tenantId,
            UUID actorId,
            UUID conversationId,
            String action,
            boolean allowTenantRead,
            Instant expiresAt) {
        return "v1|" + tenantId + "|" + actorId + "|" + conversationId + "|" + action + "|"
                + allowTenantRead + "|" + expiresAt.toEpochMilli();
    }

    static Optional<Claim> parse(String stored, Instant now) {
        if (stored == null || stored.isBlank()) {
            return Optional.empty();
        }
        String[] parts = stored.split("\\|", -1);
        if (parts.length != 7 || !"v1".equals(parts[0]) || !ACTIONS.contains(parts[4])) {
            return Optional.empty();
        }
        if (!"true".equals(parts[5]) && !"false".equals(parts[5])) {
            return Optional.empty();
        }
        try {
            Instant expiresAt = Instant.ofEpochMilli(Long.parseLong(parts[6]));
            if (!now.isBefore(expiresAt)) {
                return Optional.empty();
            }
            return Optional.of(new Claim(
                    UUID.fromString(parts[1]),
                    UUID.fromString(parts[2]),
                    UUID.fromString(parts[3]),
                    parts[4],
                    "true".equals(parts[5]),
                    expiresAt));
        } catch (IllegalArgumentException exception) {
            return Optional.empty();
        }
    }

    private TicketCommands ready() {
        if (commands == null) {
            throw unavailable("redis-not-configured");
        }
        return commands;
    }

    private static TicketCommands commandsFor(ObjectProvider<StringRedisTemplate> redis) {
        StringRedisTemplate template = redis.getIfAvailable();
        if (template == null) {
            return null;
        }
        return new RedisTicketCommands(template);
    }

    private static String newTicket() {
        byte[] bytes = new byte[32];
        RANDOM.nextBytes(bytes);
        return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
    }

    private static String sha256(String ticket) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(ticket.getBytes(StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder(digest.length * 2);
            for (byte value : digest) {
                hex.append(Character.forDigit((value >> 4) & 0xf, 16));
                hex.append(Character.forDigit(value & 0xf, 16));
            }
            return hex.toString();
        } catch (NoSuchAlgorithmException exception) {
            throw new IllegalStateException("SHA-256 不可用");
        }
    }

    private static ActorTicketException unavailable(DataAccessException exception) {
        return unavailable(exception.getClass().getSimpleName());
    }

    private static ActorTicketException unavailable(String reason) {
        log.warn("knowledge qa ticket store unavailable error={}", reason);
        return new ActorTicketException();
    }

    static final class RedisTicketCommands implements TicketCommands {
        private final StringRedisTemplate redis;

        RedisTicketCommands(StringRedisTemplate redis) {
            this.redis = redis;
        }

        @Override
        public boolean setIfAbsent(String key, String value, Duration ttl) {
            return Boolean.TRUE.equals(redis.opsForValue().setIfAbsent(key, value, ttl));
        }

        @Override
        public String getAndDelete(String key) {
            return redis.opsForValue().getAndDelete(key);
        }

        @Override
        public void delete(String key) {
            redis.delete(key);
        }

        @Override
        public int size() {
            throw new UnsupportedOperationException("ticket count is not scanned");
        }
    }
}
