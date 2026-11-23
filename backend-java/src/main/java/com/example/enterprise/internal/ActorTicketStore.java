package com.example.enterprise.internal;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.SecureRandom;
import java.time.Duration;
import java.util.Base64;
import java.util.Objects;
import java.util.Optional;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.DataAccessException;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

/**
 * One-time grant that lets Python call back without supplying a tenant or user.
 * The ticket itself is not stored. Redis keeps only the SHA-256 of the ticket
 * and the tenant and actor it was issued for. Consumption is one GETDEL, so two
 * callers cannot redeem the same ticket.
 */
@Component
public class ActorTicketStore {

    public record Grant(UUID tenantId, UUID actorId) {
    }

    interface TicketCommands {
        boolean setIfAbsent(String key, String value, Duration ttl);

        String getAndDelete(String key);

        void delete(String key);

        int size();
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

    static final Duration TTL = Duration.ofMinutes(3);
    static final String KEY_PREFIX = "actor-ticket:v1:";
    private static final int ISSUE_ATTEMPTS = 3;
    private static final SecureRandom RANDOM = new SecureRandom();
    private static final Logger log = LoggerFactory.getLogger(ActorTicketStore.class);

    private final TicketCommands commands;
    private final Duration ttl;
    private final String keyPrefix;

    @Autowired
    public ActorTicketStore(
            ObjectProvider<StringRedisTemplate> redis,
            @Value("${enterprise.actor-ticket.prefix:" + KEY_PREFIX + "}") String keyPrefix) {
        this(commandsFor(redis), TTL, keyPrefix);
    }

    ActorTicketStore(TicketCommands commands, Duration ttl, String keyPrefix) {
        if (ttl == null || ttl.isZero() || ttl.isNegative()) {
            throw new IllegalArgumentException("ticket ttl must be positive");
        }
        if (keyPrefix == null || keyPrefix.isBlank()) {
            throw new IllegalArgumentException("ticket key prefix is required");
        }
        this.commands = commands;
        this.ttl = ttl;
        this.keyPrefix = keyPrefix;
    }

    public String issue(UUID tenantId, UUID actorId) {
        TicketCommands store = ready();
        String value = Objects.requireNonNull(tenantId, "tenantId") + "|" + Objects.requireNonNull(actorId, "actorId");
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

    /**
     * Atomically redeems a live ticket. A second call, a different caller, or an
     * expired ticket receives an empty result. A Redis failure is not an empty
     * result. A value that is not a tenant and actor is rejected.
     */
    public Optional<Grant> consume(String ticket) {
        if (ticket == null || ticket.isBlank()) {
            return Optional.empty();
        }
        String stored;
        try {
            stored = ready().getAndDelete(storageKey(ticket));
        } catch (DataAccessException exception) {
            throw unavailable(exception);
        }
        return parse(stored);
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

    public int size() {
        return ready().size();
    }

    String storageKey(String ticket) {
        return keyPrefix + sha256(ticket);
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

    private static Optional<Grant> parse(String stored) {
        if (stored == null || stored.isBlank()) {
            return Optional.empty();
        }
        String[] parts = stored.split("\\|", -1);
        if (parts.length != 2) {
            return Optional.empty();
        }
        try {
            return Optional.of(new Grant(UUID.fromString(parts[0]), UUID.fromString(parts[1])));
        } catch (IllegalArgumentException exception) {
            return Optional.empty();
        }
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
        log.warn("actor ticket store unavailable error={}", reason);
        return new ActorTicketException();
    }
}
