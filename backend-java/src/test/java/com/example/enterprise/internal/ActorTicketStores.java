package com.example.enterprise.internal;

import com.example.enterprise.internal.ActorTicketStore.TicketCommands;
import java.time.Clock;
import java.time.Duration;
import org.springframework.data.redis.RedisConnectionFailureException;

/**
 * In-memory tickets for tests that do not need a Redis server.
 */
public final class ActorTicketStores {

    private ActorTicketStores() {
    }

    public static ActorTicketStore memory() {
        return store(new MemoryTicketCommands(Clock.systemUTC()));
    }

    public static ActorTicketStore failing() {
        return store(new ThrowingTicketCommands());
    }

    public static ActorTicketStore failingRevoke() {
        MemoryTicketCommands memory = new MemoryTicketCommands(Clock.systemUTC());
        return store(new TicketCommands() {
            @Override
            public boolean setIfAbsent(String key, String value, Duration ttl) {
                return memory.setIfAbsent(key, value, ttl);
            }

            @Override
            public String getAndDelete(String key) {
                return memory.getAndDelete(key);
            }

            @Override
            public void delete(String key) {
                throw new RedisConnectionFailureException("revoke-down");
            }

            @Override
            public int size() {
                return memory.size();
            }
        });
    }

    private static ActorTicketStore store(TicketCommands commands) {
        return new ActorTicketStore(commands, ActorTicketStore.TTL, "actor-ticket:memory:");
    }

    private static final class ThrowingTicketCommands implements TicketCommands {
        @Override
        public boolean setIfAbsent(String key, String value, Duration ttl) {
            throw down();
        }

        @Override
        public String getAndDelete(String key) {
            throw down();
        }

        @Override
        public void delete(String key) {
            throw down();
        }

        @Override
        public int size() {
            throw down();
        }

        private static RedisConnectionFailureException down() {
            return new RedisConnectionFailureException("redis-down");
        }
    }
}
