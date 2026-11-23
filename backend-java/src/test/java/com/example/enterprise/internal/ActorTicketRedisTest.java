package com.example.enterprise.internal;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.example.enterprise.internal.ActorTicketStore.Grant;
import io.lettuce.core.ClientOptions;
import io.lettuce.core.SocketOptions;
import java.time.Duration;
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
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.data.redis.connection.RedisStandaloneConfiguration;
import org.springframework.data.redis.connection.lettuce.LettuceClientConfiguration;
import org.springframework.data.redis.connection.lettuce.LettuceConnectionFactory;
import org.springframework.data.redis.core.Cursor;
import org.springframework.data.redis.core.ScanOptions;
import org.springframework.data.redis.core.StringRedisTemplate;

/**
 * Two store instances share one real Redis. Keys use a run-specific prefix and
 * are deleted individually. This test does not flush the database.
 */
@EnabledIfEnvironmentVariable(named = "REDIS_PASSWORD", matches = ".+")
class ActorTicketRedisTest {

    private static final String PRODUCTION_PREFIX = "actor-ticket:v1:";

    private static LettuceConnectionFactory factory;
    private static StringRedisTemplate redis;
    private static String prefix;
    private static Set<String> keysBefore;

    @BeforeAll
    static void connect() {
        String host = env("REDIS_HOST", "127.0.0.1");
        int port = Integer.parseInt(env("REDIS_PORT", "6379"));
        RedisStandaloneConfiguration configuration = new RedisStandaloneConfiguration(host, port);
        configuration.setPassword(System.getenv("REDIS_PASSWORD"));
        factory = new LettuceConnectionFactory(configuration, client(Duration.ofSeconds(2)));
        factory.afterPropertiesSet();
        redis = new StringRedisTemplate(factory);
        redis.afterPropertiesSet();
        redis.opsForValue().get(PRODUCTION_PREFIX + "connectivity");
        prefix = "actor-ticket:it:" + UUID.randomUUID() + ":";
        keysBefore = scan(PRODUCTION_PREFIX + "*");
    }

    @AfterEach
    void deleteRunKeys() {
        deletePrefix(prefix);
    }

    @AfterAll
    static void disconnect() {
        if (redis != null && prefix != null) {
            deletePrefix(prefix);
            assertEquals(keysBefore, scan(PRODUCTION_PREFIX + "*"));
            assertTrue(scan(prefix + "*").isEmpty());
        }
        if (factory != null) {
            factory.destroy();
        }
    }

    @Test
    void twoStoresShareOneTicketAndASecondConsumeFails() {
        ActorTicketStore first = store(Duration.ofSeconds(180));
        ActorTicketStore second = store(Duration.ofSeconds(180));
        UUID tenantId = UUID.randomUUID();
        UUID actorId = UUID.randomUUID();

        String ticket = first.issue(tenantId, actorId);
        String key = first.storageKey(ticket);
        assertTrue(key.startsWith(prefix));
        assertTrue(key.substring(prefix.length()).matches("[0-9a-f]{64}"));
        assertFalse(key.contains(ticket));
        Long ttl = redis.getExpire(key, TimeUnit.SECONDS);
        assertTrue(ttl != null && ttl > 150 && ttl <= 180);
        String stored = redis.opsForValue().get(key);
        assertEquals(tenantId + "|" + actorId, stored);
        assertFalse(stored.contains(ticket));

        Grant grant = second.consume(ticket).orElseThrow();
        assertEquals(tenantId, grant.tenantId());
        assertEquals(actorId, grant.actorId());
        assertFalse(redis.hasKey(key));
        assertTrue(first.consume(ticket).isEmpty());
    }

    @Test
    void sixteenConsumersAcrossTwoStoresRedeemOnce() throws Exception {
        ActorTicketStore first = store(Duration.ofSeconds(180));
        ActorTicketStore second = store(Duration.ofSeconds(180));
        UUID tenantId = UUID.randomUUID();
        UUID actorId = UUID.randomUUID();
        String ticket = first.issue(tenantId, actorId);
        int callers = 16;
        ExecutorService pool = Executors.newFixedThreadPool(callers);
        CountDownLatch start = new CountDownLatch(1);
        AtomicInteger wins = new AtomicInteger();
        List<Grant> grants = new ArrayList<>();
        try {
            List<Future<?>> waits = new ArrayList<>();
            for (int i = 0; i < callers; i++) {
                ActorTicketStore store = i % 2 == 0 ? first : second;
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
        assertEquals(new Grant(tenantId, actorId), grants.get(0));
        assertTrue(first.consume(ticket).isEmpty());
        assertTrue(second.consume(ticket).isEmpty());
    }

    @Test
    void anExpiredTicketAndARevokedTicketCannotBeConsumed() throws Exception {
        ActorTicketStore issuer = store(Duration.ofSeconds(1));
        ActorTicketStore reader = store(Duration.ofSeconds(1));
        String expired = issuer.issue(UUID.randomUUID(), UUID.randomUUID());
        Thread.sleep(2500);
        assertTrue(reader.consume(expired).isEmpty());

        ActorTicketStore live = store(Duration.ofSeconds(180));
        String ticket = live.issue(UUID.randomUUID(), UUID.randomUUID());
        live.revoke(ticket);
        live.revoke(ticket);
        assertTrue(reader.consume(ticket).isEmpty());
    }

    @Test
    void aMalformedValueIsRejectedAndRemoved() {
        ActorTicketStore store = store(Duration.ofSeconds(180));
        UUID tenantId = UUID.randomUUID();
        UUID actorId = UUID.randomUUID();
        String ticket = store.issue(tenantId, actorId);
        redis.opsForValue().set(store.storageKey(ticket), "not-a-grant", Duration.ofSeconds(30));
        assertTrue(store.consume(ticket).isEmpty());
        assertFalse(redis.hasKey(store.storageKey(ticket)));
    }

    @Test
    void aClosedRedisFailsIssueConsumeAndRevoke() {
        LettuceConnectionFactory closed = new LettuceConnectionFactory(
                new RedisStandaloneConfiguration("127.0.0.1", 1), client(Duration.ofMillis(300)));
        closed.afterPropertiesSet();
        try {
            StringRedisTemplate unavailable = new StringRedisTemplate(closed);
            unavailable.afterPropertiesSet();
            ActorTicketStore store = new ActorTicketStore(
                    new ActorTicketStore.RedisTicketCommands(unavailable),
                    Duration.ofSeconds(180),
                    prefix);
            assertThrows(ActorTicketException.class, () -> store.issue(UUID.randomUUID(), UUID.randomUUID()));
            assertThrows(ActorTicketException.class, () -> store.consume("ticket-present"));
            assertThrows(ActorTicketException.class, () -> store.revoke("ticket-present"));
        } finally {
            closed.destroy();
        }
    }

    private static ActorTicketStore store(Duration ttl) {
        return new ActorTicketStore(new ActorTicketStore.RedisTicketCommands(redis), ttl, prefix);
    }

    private static LettuceClientConfiguration client(Duration timeout) {
        return LettuceClientConfiguration.builder()
                .commandTimeout(timeout)
                .clientOptions(ClientOptions.builder()
                        .socketOptions(SocketOptions.builder().connectTimeout(timeout).build())
                        .build())
                .build();
    }

    private static void deletePrefix(String keyPrefix) {
        List<String> keys = new ArrayList<>(scan(keyPrefix + "*"));
        if (!keys.isEmpty()) {
            redis.delete(keys);
        }
    }

    private static Set<String> scan(String pattern) {
        Set<String> names = new HashSet<>();
        ScanOptions options = ScanOptions.scanOptions().match(pattern).count(200).build();
        try (Cursor<String> cursor = redis.scan(options)) {
            cursor.forEachRemaining(names::add);
        }
        return names;
    }

    private static String env(String name, String fallback) {
        String value = System.getenv(name);
        return value == null || value.isBlank() ? fallback : value;
    }
}
