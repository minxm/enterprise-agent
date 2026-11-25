package com.example.enterprise.internal;

import java.time.Clock;

public final class KnowledgeQaTicketStores {

    private KnowledgeQaTicketStores() {
    }

    public static KnowledgeQaTicketStore memory() {
        return memory(Clock.systemUTC());
    }

    public static KnowledgeQaTicketStore memory(Clock clock) {
        return new KnowledgeQaTicketStore(
                new MemoryTicketCommands(clock),
                KnowledgeQaTicketStore.TTL,
                KnowledgeQaTicketStore.KEY_PREFIX,
                clock);
    }

    public static KnowledgeQaTicketStore memory(
            ActorTicketStore.TicketCommands commands, Clock clock) {
        return new KnowledgeQaTicketStore(
                commands, KnowledgeQaTicketStore.TTL, KnowledgeQaTicketStore.KEY_PREFIX, clock);
    }
}
