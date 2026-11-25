package com.example.enterprise.internal;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.example.enterprise.internal.KnowledgeQaTicketStore.Claim;
import io.lettuce.core.ClientOptions;
import io.lettuce.core.SocketOptions;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
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
 * Real Redis consumption for Knowledge QA tickets. Keys use a run-specific
 * prefix and are deleted individually. This test does not flush the database.
 */
@EnabledIfEnvironmentVariable(named = "REDIS_PASSWORD", matches = ".+")
class KnowledgeQaTicketRedisTest {

    private static final String PRODUCTION_PREFIX = "knowledge-qa-ticket:v1:";
    private static final String CONTRACT_PREFIX = "actor-ticket:v1:";

    private static LettuceConnectionFactory factory;
    private static StringRedisTemplate redis;
    private static String prefix;
    private static Set<String> knowledgeBefore;
    private static Set<String> contractBefore;

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
        prefix = "knowledge-qa-ticket:it:" + UUID.randomUUID() + ":";
        knowledgeBefore = scan(PRODUCTION_PREFIX + "*");
        contractBefore = scan(CONTRACT_PREFIX + "*");
    }

    @AfterEach
    void deleteRunKeys() {
        deletePrefix(prefix);
    }

    @AfterAll
    static void disconnect() {
        if (redis != null && prefix != null) {
            deletePrefix(prefix);
            assertEquals(knowledgeBefore, scan(PRODUCTION_PREFIX + "*"));
            assertEquals(contractBefore, scan(CONTRACT_PREFIX + "*"));
            assertTrue(scan(prefix + "*").isEmpty());
        }
        if (factory != null) {
            factory.destroy();
        }
    }

    @Test
    void redeemConsumesOnceAndDoesNotTouchContractTickets() {
        KnowledgeQaTicketStore first = store(Duration.ofSeconds(180));
        KnowledgeQaTicketStore second = store(Duration.ofSeconds(180));
        ActorTicketStore contracts = new ActorTicketStore(
                new ActorTicketStore.RedisTicketCommands(redis), Duration.ofMinutes(3), prefix + "contract:");
        UUID tenantId = UUID.randomUUID();
        UUID actorId = UUID.randomUUID();
        UUID conversationId = UUID.randomUUID();

        String knowledge = first.issue(
                tenantId, actorId, conversationId, KnowledgeQaTicketStore.ACTION_STREAM, false);
        String contract = contracts.issue(tenantId, actorId);
        String key = first.storageKey(knowledge);
        assertTrue(key.startsWith(prefix));
        assertFalse(key.contains(knowledge));
        String stored = redis.opsForValue().get(key);
        assertTrue(stored.startsWith("v1|" + tenantId + "|" + actorId + "|" + conversationId));
        assertFalse(stored.contains(knowledge));

        Claim claim = second.consume(knowledge).orElseThrow();
        assertEquals(conversationId, claim.conversationId());
        assertEquals(KnowledgeQaTicketStore.ACTION_STREAM, claim.action());
        assertTrue(first.consume(knowledge).isEmpty());
        assertTrue(second.consume(contract).isEmpty());
        assertEquals(actorId, contracts.consume(contract).orElseThrow().actorId());
    }

    @Test
    void expiredTicketIsRejected() throws Exception {
        KnowledgeQaTicketStore store = store(Duration.ofSeconds(1));
        String ticket = store.issue(
                UUID.randomUUID(), UUID.randomUUID(), UUID.randomUUID(),
                KnowledgeQaTicketStore.ACTION_HISTORY, true);
        TimeUnit.MILLISECONDS.sleep(1200);
        assertTrue(store.consume(ticket).isEmpty());
    }

    @Test
    void closedRedisFailsClosed() {
        LettuceConnectionFactory closed = new LettuceConnectionFactory(
                new RedisStandaloneConfiguration("127.0.0.1", 1), client(Duration.ofMillis(300)));
        closed.afterPropertiesSet();
        try {
            StringRedisTemplate unavailable = new StringRedisTemplate(closed);
            unavailable.afterPropertiesSet();
            KnowledgeQaTicketStore store = new KnowledgeQaTicketStore(
                    new KnowledgeQaTicketStore.RedisTicketCommands(unavailable),
                    Duration.ofSeconds(180),
                    prefix,
                    java.time.Clock.systemUTC());
            assertThrows(ActorTicketException.class, () -> store.issue(
                    UUID.randomUUID(), UUID.randomUUID(), UUID.randomUUID(),
                    KnowledgeQaTicketStore.ACTION_STREAM, false));
            assertThrows(ActorTicketException.class, () -> store.consume("present"));
            assertThrows(ActorTicketException.class, () -> store.revoke("present"));
        } finally {
            closed.destroy();
        }
    }

    private static KnowledgeQaTicketStore store(Duration ttl) {
        return new KnowledgeQaTicketStore(
                new KnowledgeQaTicketStore.RedisTicketCommands(redis),
                ttl,
                prefix,
                java.time.Clock.systemUTC());
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
