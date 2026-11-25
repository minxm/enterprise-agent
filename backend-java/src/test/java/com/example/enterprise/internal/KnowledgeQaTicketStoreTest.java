package com.example.enterprise.internal;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.example.enterprise.internal.KnowledgeQaTicketStore.Claim;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.dao.QueryTimeoutException;

class KnowledgeQaTicketStoreTest {

    private static final UUID TENANT = UUID.fromString("00000000-0000-4000-8000-0000000000a1");
    private static final UUID OTHER_TENANT = UUID.fromString("00000000-0000-4000-8000-0000000000a2");
    private static final UUID ACTOR = UUID.fromString("00000000-0000-4000-8000-0000000000b1");
    private static final UUID OTHER_ACTOR = UUID.fromString("00000000-0000-4000-8000-0000000000b2");
    private static final UUID CONVERSATION = UUID.fromString("00000000-0000-4000-8000-0000000000c1");
    private static final UUID OTHER_CONVERSATION = UUID.fromString("00000000-0000-4000-8000-0000000000c2");

    @Test
    void issuesStreamAndHistoryTicketsAndConsumesEachOnce() {
        MutableClock clock = new MutableClock(Instant.parse("2026-11-25T08:00:00Z"));
        KnowledgeQaTicketStore store = store(clock);

        String stream = store.issue(TENANT, ACTOR, CONVERSATION, KnowledgeQaTicketStore.ACTION_STREAM, false);
        String history = store.issue(OTHER_TENANT, OTHER_ACTOR, OTHER_CONVERSATION,
                KnowledgeQaTicketStore.ACTION_HISTORY, true);

        Claim streamClaim = store.consume(stream).orElseThrow();
        assertEquals(TENANT, streamClaim.tenantId());
        assertEquals(ACTOR, streamClaim.actorId());
        assertEquals(CONVERSATION, streamClaim.conversationId());
        assertEquals(KnowledgeQaTicketStore.ACTION_STREAM, streamClaim.action());
        assertFalse(streamClaim.allowTenantRead());

        Claim historyClaim = store.consume(history).orElseThrow();
        assertEquals(OTHER_TENANT, historyClaim.tenantId());
        assertEquals(OTHER_ACTOR, historyClaim.actorId());
        assertEquals(OTHER_CONVERSATION, historyClaim.conversationId());
        assertEquals(KnowledgeQaTicketStore.ACTION_HISTORY, historyClaim.action());
        assertTrue(historyClaim.allowTenantRead());

        assertTrue(store.consume(stream).isEmpty());
        assertTrue(store.consume(history).isEmpty());
    }

    @Test
    void expiredOrUnknownTicketsAreRejected() {
        MutableClock clock = new MutableClock(Instant.parse("2026-11-25T08:00:00Z"));
        KnowledgeQaTicketStore store = store(clock);
        String ticket = store.issue(TENANT, ACTOR, CONVERSATION, KnowledgeQaTicketStore.ACTION_STREAM, false);
        clock.advance(Duration.ofSeconds(181));
        assertTrue(store.consume(ticket).isEmpty());
        assertTrue(store.consume("not-a-ticket").isEmpty());
        assertTrue(store.consume(" ").isEmpty());
    }

    @Test
    void malformedStoredValueIsNotAGrant() {
        MutableClock clock = new MutableClock(Instant.parse("2026-11-25T08:00:00Z"));
        MemoryTicketCommands commands = new MemoryTicketCommands(clock);
        KnowledgeQaTicketStore store = KnowledgeQaTicketStores.memory(commands, clock);
        String ticket = store.issue(TENANT, ACTOR, CONVERSATION, KnowledgeQaTicketStore.ACTION_STREAM, false);
        commands.replaceLast(TENANT + "|" + ACTOR);
        assertTrue(store.consume(ticket).isEmpty());
    }

    @Test
    void differentActionUserTenantAndConversationStayBound() {
        KnowledgeQaTicketStore store = store(new MutableClock(Instant.parse("2026-11-25T08:00:00Z")));
        String ticket = store.issue(TENANT, ACTOR, CONVERSATION, KnowledgeQaTicketStore.ACTION_STREAM, false);
        Claim claim = store.consume(ticket).orElseThrow();
        assertFalse(claim.action().equals(KnowledgeQaTicketStore.ACTION_HISTORY));
        assertFalse(claim.tenantId().equals(OTHER_TENANT));
        assertFalse(claim.actorId().equals(OTHER_ACTOR));
        assertFalse(claim.conversationId().equals(OTHER_CONVERSATION));
    }

    @Test
    void redisFailureDoesNotReturnAGrant() {
        ActorTicketStore.TicketCommands commands = new ActorTicketStore.TicketCommands() {
            @Override
            public boolean setIfAbsent(String key, String value, java.time.Duration ttl) {
                throw new QueryTimeoutException("down");
            }

            @Override
            public String getAndDelete(String key) {
                throw new QueryTimeoutException("down");
            }

            @Override
            public void delete(String key) {
                throw new QueryTimeoutException("down");
            }

            @Override
            public int size() {
                return 0;
            }
        };
        KnowledgeQaTicketStore store = KnowledgeQaTicketStores.memory(commands, Clock.systemUTC());
        assertThrows(ActorTicketException.class, () -> store.issue(
                TENANT, ACTOR, CONVERSATION, KnowledgeQaTicketStore.ACTION_STREAM, false));
        assertThrows(ActorTicketException.class, () -> store.consume("present"));
        assertThrows(ActorTicketException.class, () -> store.revoke("present"));
    }

    @Test
    void contractTicketValueCannotBeConsumedAsKnowledgeGrant() {
        MutableClock clock = new MutableClock(Instant.parse("2026-11-25T08:00:00Z"));
        MemoryTicketCommands commands = new MemoryTicketCommands(clock);
        KnowledgeQaTicketStore knowledge = KnowledgeQaTicketStores.memory(commands, clock);
        ActorTicketStore contracts = new ActorTicketStore(commands, Duration.ofMinutes(3), "actor-ticket:v1:");
        String contractTicket = contracts.issue(TENANT, ACTOR);
        assertTrue(knowledge.consume(contractTicket).isEmpty());
        ActorTicketStore.Grant grant = contracts.consume(contractTicket).orElseThrow();
        assertEquals(TENANT, grant.tenantId());
        assertEquals(ACTOR, grant.actorId());
    }

    private static KnowledgeQaTicketStore store(Clock clock) {
        return KnowledgeQaTicketStores.memory(clock);
    }

    private static final class MutableClock extends Clock {
        private Instant instant;

        private MutableClock(Instant instant) {
            this.instant = instant;
        }

        private void advance(Duration duration) {
            instant = instant.plus(duration);
        }

        @Override
        public ZoneOffset getZone() {
            return ZoneOffset.UTC;
        }

        @Override
        public Clock withZone(java.time.ZoneId zone) {
            return this;
        }

        @Override
        public Instant instant() {
            return instant;
        }
    }
}
