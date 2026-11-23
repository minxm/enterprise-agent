package com.example.enterprise.internal;

import com.example.enterprise.internal.ActorTicketStore.TicketCommands;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Atomic stand-in for the Redis commands. Production code never uses it.
 */
final class MemoryTicketCommands implements TicketCommands {

    private final Clock clock;
    private final ConcurrentHashMap<String, Stored> data = new ConcurrentHashMap<>();
    private String lastKey;
    private Duration lastTtl;

    MemoryTicketCommands(Clock clock) {
        this.clock = clock;
    }

    @Override
    public boolean setIfAbsent(String key, String value, Duration ttl) {
        lastKey = key;
        lastTtl = ttl;
        Stored created = new Stored(value, clock.instant().plus(ttl));
        return data.putIfAbsent(key, created) == null;
    }

    @Override
    public String getAndDelete(String key) {
        Stored[] removed = new Stored[1];
        data.compute(key, (ignored, existing) -> {
            removed[0] = existing;
            return null;
        });
        if (removed[0] == null || !clock.instant().isBefore(removed[0].expiresAt())) {
            return null;
        }
        return removed[0].value();
    }

    @Override
    public void delete(String key) {
        data.remove(key);
    }

    @Override
    public int size() {
        Instant now = clock.instant();
        int live = 0;
        for (Stored stored : data.values()) {
            if (now.isBefore(stored.expiresAt())) {
                live++;
            }
        }
        return live;
    }

    String lastKey() {
        return lastKey;
    }

    Duration lastTtl() {
        return lastTtl;
    }

    void replaceLast(String value) {
        data.computeIfPresent(lastKey, (key, stored) -> new Stored(value, stored.expiresAt()));
    }

    private record Stored(String value, Instant expiresAt) {
    }
}
