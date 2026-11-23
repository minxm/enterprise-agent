package com.example.enterprise.internal;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

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
import org.springframework.data.redis.RedisConnectionFailureException;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.ValueOperations;

class ActorTicketStoreTest {

    @Test
    void ticketsAreRandomUrlSafeValuesWithAThreeMinuteLifetime() {
        MemoryTicketCommands redis = new MemoryTicketCommands(Clock.systemUTC());
        ActorTicketStore store = store(redis);
        Set<String> issued = new HashSet<>();
        for (int i = 0; i < 32; i++) {
            String ticket = store.issue(UUID.randomUUID(), UUID.randomUUID());
            assertTrue(ticket.matches("[A-Za-z0-9_-]{43}"));
            assertTrue(issued.add(ticket));
            assertEquals(ActorTicketStore.TTL, redis.lastTtl());
            assertFalse(redis.lastKey().contains(ticket));
            assertTrue(redis.lastKey().matches("actor-ticket:v1:[0-9a-f]{64}"));
        }
        assertEquals(ActorTicketStore.TTL, Duration.ofMinutes(3));
        assertEquals(Duration.ofSeconds(180), ActorTicketStore.TTL);
        assertEquals(ActorTicketStore.KEY_PREFIX, "actor-ticket:v1:");
    }

    @Test
    void anExpiredTicketIsRejected() {
        MutableClock clock = new MutableClock(Instant.parse("2026-11-19T06:00:00Z"));
        ActorTicketStore store = store(new MemoryTicketCommands(clock));
        UUID tenantId = UUID.randomUUID();
        UUID userId = UUID.randomUUID();
        String ticket = store.issue(tenantId, userId);

        clock.advance(Duration.ofSeconds(179));
        assertEquals(tenantId, store.consume(ticket).orElseThrow().tenantId());

        String expired = store.issue(tenantId, userId);
        clock.advance(Duration.ofMinutes(3));
        assertTrue(store.consume(expired).isEmpty());
        assertEquals(0, store.size());
    }

    @Test
    void aTicketCanBeConsumedOnlyOnceAndKeepsItsIdentity() {
        ActorTicketStore store = store(new MemoryTicketCommands(Clock.systemUTC()));
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
    void revokeRemovesTheTicketAndCanBeRepeated() {
        ActorTicketStore store = store(new MemoryTicketCommands(Clock.systemUTC()));
        String ticket = store.issue(UUID.randomUUID(), UUID.randomUUID());
        store.revoke(ticket);
        assertTrue(store.consume(ticket).isEmpty());
        store.revoke(ticket);
        store.revoke(null);
        store.revoke(" ");
        assertEquals(0, store.size());
    }

    @Test
    void aMalformedRedisValueDoesNotBecomeAGrant() {
        MemoryTicketCommands redis = new MemoryTicketCommands(Clock.systemUTC());
        ActorTicketStore store = store(redis);
        String ticket = store.issue(UUID.randomUUID(), UUID.randomUUID());
        redis.replaceLast("not-a-grant");
        assertTrue(store.consume(ticket).isEmpty());

        String second = store.issue(UUID.randomUUID(), UUID.randomUUID());
        redis.replaceLast(UUID.randomUUID() + "|");
        assertTrue(store.consume(second).isEmpty());
        assertEquals(0, store.size());
    }

    @Test
    void aRedisFailureDoesNotReturnAGrant() {
        ActorTicketStore store = ActorTicketStores.failing();
        UUID tenantId = UUID.randomUUID();
        UUID actorId = UUID.randomUUID();
        ActorTicketException issue = assertThrows(ActorTicketException.class, () -> store.issue(tenantId, actorId));
        assertEquals(503, issue.getStatus().value());
        assertEquals("Ticket 存储不可用", issue.getMessage());
        assertNull(issue.getCause());
        assertFalse(issue.getMessage().contains(tenantId.toString()));

        ActorTicketException consume = assertThrows(ActorTicketException.class, () -> store.consume("present"));
        assertEquals(503, consume.getStatus().value());
        ActorTicketException revoke = assertThrows(ActorTicketException.class, () -> store.revoke("present"));
        assertEquals(503, revoke.getStatus().value());
        assertTrue(store.consume(" ").isEmpty());
    }

    @Test
    void concurrentConsumersRedeemATicketOnce() throws Exception {
        ActorTicketStore store = store(new MemoryTicketCommands(Clock.systemUTC()));
        UUID tenantId = UUID.randomUUID();
        UUID actorId = UUID.randomUUID();
        String ticket = store.issue(tenantId, actorId);
        int callers = 16;
        ExecutorService pool = Executors.newFixedThreadPool(callers);
        CountDownLatch start = new CountDownLatch(1);
        AtomicInteger wins = new AtomicInteger();
        List<Grant> grants = new ArrayList<>();
        try {
            List<Future<?>> waits = new ArrayList<>();
            for (int i = 0; i < callers; i++) {
                waits.add(pool.submit(() -> {
                    start.await();
                    Optional<Grant> grant = store.consume(ticket);
                    grant.ifPresent(value -> {
                        wins.incrementAndGet();
                        synchronized (grants) {
                            grants.add(value);
                        }
                    });
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
        assertEquals(1, grants.size());
        assertEquals(tenantId, grants.get(0).tenantId());
        assertEquals(actorId, grants.get(0).actorId());
        assertTrue(store.consume(ticket).isEmpty());
        assertEquals(0, store.size());
    }

    @Test
    void setNxMustSucceedBeforeTheTicketIsReturned() {
        StringRedisTemplate redis = mock(StringRedisTemplate.class);
        @SuppressWarnings("unchecked")
        ValueOperations<String, String> values = mock(ValueOperations.class);
        when(redis.opsForValue()).thenReturn(values);
        when(values.setIfAbsent(any(), any(), eq(ActorTicketStore.TTL))).thenReturn(null, Boolean.FALSE, Boolean.TRUE);
        ActorTicketStore store = new ActorTicketStore(
                new ActorTicketStore.RedisTicketCommands(redis), ActorTicketStore.TTL, ActorTicketStore.KEY_PREFIX);

        String ticket = store.issue(UUID.randomUUID(), UUID.randomUUID());

        assertTrue(ticket.matches("[A-Za-z0-9_-]{43}"));
        verify(values, times(3)).setIfAbsent(any(), any(), eq(ActorTicketStore.TTL));
    }

    @Test
    void redisCommandFailuresStayInfrastructureErrors() {
        StringRedisTemplate redis = mock(StringRedisTemplate.class);
        @SuppressWarnings("unchecked")
        ValueOperations<String, String> values = mock(ValueOperations.class);
        when(redis.opsForValue()).thenReturn(values);
        when(values.setIfAbsent(any(), any(), eq(ActorTicketStore.TTL)))
                .thenThrow(new RedisConnectionFailureException("secret-ticket-value"));
        when(values.getAndDelete(any())).thenThrow(new RedisConnectionFailureException("secret-ticket-value"));
        when(redis.delete(any(String.class))).thenThrow(new RedisConnectionFailureException("secret-ticket-value"));
        ActorTicketStore store = new ActorTicketStore(
                new ActorTicketStore.RedisTicketCommands(redis), ActorTicketStore.TTL, ActorTicketStore.KEY_PREFIX);

        ActorTicketException issue = assertThrows(ActorTicketException.class,
                () -> store.issue(UUID.randomUUID(), UUID.randomUUID()));
        ActorTicketException consume = assertThrows(ActorTicketException.class, () -> store.consume("ticket-value"));
        ActorTicketException revoke = assertThrows(ActorTicketException.class, () -> store.revoke("ticket-value"));
        assertNull(issue.getCause());
        assertNull(consume.getCause());
        assertNull(revoke.getCause());
        assertFalse(issue.getMessage().contains("secret-ticket-value"));
        assertFalse(consume.getMessage().contains("ticket-value"));
        assertFalse(revoke.getMessage().contains("secret-ticket-value"));
    }

    private static ActorTicketStore store(MemoryTicketCommands commands) {
        return new ActorTicketStore(commands, ActorTicketStore.TTL, ActorTicketStore.KEY_PREFIX);
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
