package com.example.enterprise.internal;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.example.enterprise.internal.ActorTicketStore.Grant;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneId;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;

class ActorTicketStoreTest {

    @Test
    void ticketsAreRandomUrlSafeValuesWithAThreeMinuteLifetime() {
        ActorTicketStore store = new ActorTicketStore();
        Set<String> issued = new HashSet<>();
        for (int i = 0; i < 32; i++) {
            String ticket = store.issue(UUID.randomUUID(), UUID.randomUUID());
            assertTrue(ticket.matches("[A-Za-z0-9_-]{43}"));
            assertTrue(issued.add(ticket));
        }
        assertEquals(ActorTicketStore.TTL, Duration.ofMinutes(3));
    }

    @Test
    void anExpiredTicketIsRejected() {
        MutableClock clock = new MutableClock(Instant.parse("2026-11-19T06:00:00Z"));
        ActorTicketStore store = new ActorTicketStore(clock);
        UUID tenantId = UUID.randomUUID();
        UUID userId = UUID.randomUUID();
        String ticket = store.issue(tenantId, userId);

        clock.advance(Duration.ofMinutes(3));
        assertEquals(tenantId, store.consume(ticket).orElseThrow().tenantId());

        String expired = store.issue(tenantId, userId);
        clock.advance(Duration.ofMinutes(3).plusSeconds(1));
        assertTrue(store.consume(expired).isEmpty());
        assertEquals(0, store.size());
    }

    @Test
    void aTicketCanBeConsumedOnlyOnceAndKeepsItsIdentity() {
        ActorTicketStore store = new ActorTicketStore();
        UUID tenantA = UUID.randomUUID();
        UUID userA = UUID.randomUUID();
        UUID tenantB = UUID.randomUUID();
        UUID userB = UUID.randomUUID();
        String ticketA = store.issue(tenantA, userA);
        String ticketB = store.issue(tenantB, userB);

        Grant grant = store.consume(ticketA).orElseThrow();
        assertEquals(tenantA, grant.tenantId());
        assertEquals(userA, grant.actorId());
        assertNotEquals(tenantB, grant.tenantId());
        assertNotEquals(userB, grant.actorId());
        assertTrue(store.consume(ticketA).isEmpty());
        assertTrue(store.consume(" ").isEmpty());
        assertTrue(store.consume(null).isEmpty());

        Grant other = store.consume(ticketB).orElseThrow();
        assertEquals(tenantB, other.tenantId());
        assertEquals(userB, other.actorId());
        assertTrue(store.consume(ticketB).isEmpty());
    }

    @Test
    void concurrentConsumersRedeemATicketOnce() throws Exception {
        ActorTicketStore store = new ActorTicketStore();
        String ticket = store.issue(UUID.randomUUID(), UUID.randomUUID());
        int callers = 16;
        ExecutorService pool = Executors.newFixedThreadPool(callers);
        CountDownLatch start = new CountDownLatch(1);
        AtomicInteger wins = new AtomicInteger();
        try {
            List<Future<?>> waits = new ArrayList<>();
            for (int i = 0; i < callers; i++) {
                waits.add(pool.submit(() -> {
                    start.await();
                    Optional<Grant> grant = store.consume(ticket);
                    if (grant.isPresent()) {
                        wins.incrementAndGet();
                    }
                    return null;
                }));
            }
            start.countDown();
            for (Future<?> wait : waits) {
                wait.get();
            }
        } finally {
            pool.shutdownNow();
        }
        assertEquals(1, wins.get());
        assertTrue(store.consume(ticket).isEmpty());
        assertEquals(0, store.size());
    }

    private static final class MutableClock extends Clock {
        private Instant current;

        private MutableClock(Instant current) {
            this.current = current;
        }

        private void advance(Duration duration) {
            current = current.plus(duration);
        }

        @Override
        public ZoneId getZone() {
            return ZoneId.of("UTC");
        }

        @Override
        public Clock withZone(ZoneId zone) {
            return this;
        }

        @Override
        public Instant instant() {
            return current;
        }
    }
}
