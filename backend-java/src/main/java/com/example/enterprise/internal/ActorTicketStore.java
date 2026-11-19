package com.example.enterprise.internal;

import java.security.SecureRandom;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.Base64;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import org.springframework.stereotype.Component;

/**
 * One-time grant that lets Python call back without supplying a tenant or user.
 * The ticket is the only identity Java accepts on the internal contract read.
 * Consumption removes the grant, so two callers cannot redeem the same ticket.
 */
@Component
public class ActorTicketStore {

    public record Grant(UUID tenantId, UUID actorId) {
    }

    static final Duration TTL = Duration.ofMinutes(3);
    private static final SecureRandom RANDOM = new SecureRandom();

    private final Clock clock;
    private final ConcurrentHashMap<String, Stored> grants = new ConcurrentHashMap<>();

    public ActorTicketStore() {
        this(Clock.systemUTC());
    }

    ActorTicketStore(Clock clock) {
        this.clock = clock;
    }

    public String issue(UUID tenantId, UUID actorId) {
        purgeExpired();
        byte[] bytes = new byte[32];
        RANDOM.nextBytes(bytes);
        String ticket = Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
        grants.put(ticket, new Stored(tenantId, actorId, clock.instant().plus(TTL)));
        return ticket;
    }

    /**
     * Atomically redeems a live ticket. A second call, a different caller, or an
     * expired ticket receives an empty result. The grant's tenant and user are
     * the ones recorded at issue time.
     */
    public Optional<Grant> consume(String ticket) {
        if (ticket == null || ticket.isBlank()) {
            return Optional.empty();
        }
        Stored stored = grants.remove(ticket);
        if (stored == null || stored.expiresAt().isBefore(clock.instant())) {
            return Optional.empty();
        }
        return Optional.of(new Grant(stored.tenantId(), stored.actorId()));
    }

    public void revoke(String ticket) {
        if (ticket != null) {
            grants.remove(ticket);
        }
    }

    public int size() {
        return grants.size();
    }

    private void purgeExpired() {
        Instant now = clock.instant();
        grants.entrySet().removeIf(entry -> entry.getValue().expiresAt().isBefore(now));
    }

    private record Stored(UUID tenantId, UUID actorId, Instant expiresAt) {
    }
}
