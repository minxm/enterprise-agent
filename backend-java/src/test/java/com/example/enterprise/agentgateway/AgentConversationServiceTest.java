package com.example.enterprise.agentgateway;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.example.enterprise.identity.IdentityException;
import java.time.Instant;
import java.util.Comparator;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.http.HttpStatus;

class AgentConversationServiceTest {

    @Test
    void ownershipFollowsTheServerTenantAndUser() {
        MemoryStore store = new MemoryStore();
        AgentConversationService service = new AgentConversationService(provider(store));
        UUID tenant = UUID.randomUUID();
        UUID owner = UUID.randomUUID();
        UUID otherUser = UUID.randomUUID();
        UUID otherTenant = UUID.randomUUID();

        UUID conversationId = service.create(tenant, owner);

        service.requireOwned(tenant, owner, conversationId);
        assertThatThrownBy(() -> service.requireOwned(tenant, otherUser, conversationId))
                .isInstanceOf(IdentityException.class)
                .hasMessage("会话不存在")
                .extracting(exception -> ((IdentityException) exception).status())
                .isEqualTo(HttpStatus.NOT_FOUND);
        assertThatThrownBy(() -> service.requireOwned(otherTenant, owner, conversationId))
                .isInstanceOf(IdentityException.class)
                .hasMessage("会话不存在");
        assertThat(store.lastTenant).isEqualTo(otherTenant);
    }

    @Test
    void listsOnlyTheRequestedTenantAndOwnerNewestFirst() {
        MemoryStore store = new MemoryStore();
        AgentConversationService service = new AgentConversationService(provider(store));
        UUID tenant = UUID.randomUUID();
        UUID owner = UUID.randomUUID();
        UUID otherUser = UUID.randomUUID();
        UUID otherTenant = UUID.randomUUID();

        UUID oldest = service.create(tenant, owner);
        UUID middle = service.create(tenant, owner);
        service.create(tenant, otherUser);
        service.create(otherTenant, owner);
        UUID newest = service.create(tenant, owner);

        ConversationPage first = service.list(tenant, owner, 0, 2);
        ConversationPage second = service.list(tenant, owner, 1, 2);

        assertThat(first.total()).isEqualTo(3);
        assertThat(first.items()).extracting(ConversationSummary::conversationId).containsExactly(newest, middle);
        assertThat(second.items()).extracting(ConversationSummary::conversationId).containsExactly(oldest);
        assertThat(service.list(tenant, otherUser, 0, 20).total()).isEqualTo(1);
        assertThat(service.list(otherTenant, owner, 0, 20).total()).isEqualTo(1);
    }

    @Test
    void rejectsAnIllegalConversationPage() {
        AgentConversationService service = new AgentConversationService(provider(new MemoryStore()));

        assertThatThrownBy(() -> service.list(UUID.randomUUID(), UUID.randomUUID(), -1, 20))
                .isInstanceOf(IdentityException.class)
                .hasMessage("请求参数不合法")
                .extracting(exception -> ((IdentityException) exception).status())
                .isEqualTo(HttpStatus.BAD_REQUEST);
        assertThatThrownBy(() -> service.list(UUID.randomUUID(), UUID.randomUUID(), 0, 101))
                .isInstanceOf(IdentityException.class)
                .hasMessage("请求参数不合法");
    }

    @Test
    void reportsThatConversationsAreDisabledWithoutAStore() {
        AgentConversationService service = new AgentConversationService(provider(null));

        assertThatThrownBy(() -> service.list(UUID.randomUUID(), UUID.randomUUID(), 0, 20))
                .isInstanceOf(IdentityException.class)
                .hasMessage("会话功能未启用");
        assertThatThrownBy(() -> service.create(UUID.randomUUID(), UUID.randomUUID()))
                .isInstanceOf(IdentityException.class)
                .hasMessage("会话功能未启用")
                .extracting(exception -> ((IdentityException) exception).status())
                .isEqualTo(HttpStatus.SERVICE_UNAVAILABLE);
    }

    private static ObjectProvider<AgentConversationStore> provider(AgentConversationStore store) {
        return new ObjectProvider<>() {
            @Override
            public AgentConversationStore getObject() {
                return store;
            }

            @Override
            public AgentConversationStore getObject(Object... args) {
                return store;
            }

            @Override
            public AgentConversationStore getIfAvailable() {
                return store;
            }

            @Override
            public AgentConversationStore getIfUnique() {
                return store;
            }
        };
    }

    private static final class MemoryStore implements AgentConversationStore {
        private final ConcurrentHashMap<UUID, Row> rows = new ConcurrentHashMap<>();
        private Instant clock = Instant.parse("2026-01-01T00:00:00Z");
        private UUID lastTenant;

        @Override
        public UUID insert(UUID tenantId, UUID ownerUserId) {
            lastTenant = tenantId;
            clock = clock.plusSeconds(1);
            UUID id = UUID.randomUUID();
            rows.put(id, new Row(tenantId, ownerUserId, clock));
            return id;
        }

        @Override
        public boolean ownedBy(UUID tenantId, UUID ownerUserId, UUID conversationId) {
            lastTenant = tenantId;
            Row row = rows.get(conversationId);
            return row != null && row.tenantId().equals(tenantId) && row.ownerUserId().equals(ownerUserId);
        }

        @Override
        public List<ConversationSummary> list(UUID tenantId, UUID ownerUserId, int limit, long offset) {
            lastTenant = tenantId;
            return rows.entrySet().stream()
                    .filter(entry -> entry.getValue().tenantId().equals(tenantId)
                            && entry.getValue().ownerUserId().equals(ownerUserId))
                    .sorted(Comparator.comparing((java.util.Map.Entry<UUID, Row> entry) -> entry.getValue().createdAt())
                            .reversed()
                            .thenComparing(entry -> entry.getKey().toString(), Comparator.reverseOrder()))
                    .skip(offset)
                    .limit(limit)
                    .map(entry -> new ConversationSummary(entry.getKey(), entry.getValue().createdAt()))
                    .toList();
        }

        @Override
        public long count(UUID tenantId, UUID ownerUserId) {
            lastTenant = tenantId;
            return rows.values().stream()
                    .filter(row -> row.tenantId().equals(tenantId) && row.ownerUserId().equals(ownerUserId))
                    .count();
        }
    }

    private record Row(UUID tenantId, UUID ownerUserId, Instant createdAt) {
    }
}
