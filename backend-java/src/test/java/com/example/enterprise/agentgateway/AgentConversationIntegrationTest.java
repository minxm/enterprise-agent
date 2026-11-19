package com.example.enterprise.agentgateway;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.example.enterprise.identity.IdentityException;
import com.example.enterprise.identity.IdentityService;
import com.example.enterprise.identity.IdentityViews.TenantView;
import com.example.enterprise.identity.IdentityViews.UserView;
import com.example.enterprise.identity.TenantContext;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.transaction.annotation.Transactional;

@SpringBootTest
@ActiveProfiles("dev")
@Transactional
@EnabledIfEnvironmentVariable(named = "BUSINESS_APP_PASSWORD", matches = ".+")
class AgentConversationIntegrationTest {

    @Autowired
    private IdentityService identity;

    @Autowired
    private JdbcAgentConversationStore store;

    @Autowired
    private AgentConversationService conversations;

    @Autowired
    private JdbcTemplate jdbc;

    @AfterEach
    void clear() {
        TenantContext.clear();
        SecurityContextHolder.clearContext();
    }

    @Test
    void aConversationIsVisibleOnlyToItsOwnerInsideTheTenant() {
        String suffix = UUID.randomUUID().toString().substring(0, 8);
        TenantView tenant = identity.createTenant("qa-" + suffix, "QA " + suffix);
        TenantContext.set(tenant.id());
        UserView owner = identity.createUser("owner-" + suffix, "plain-secret-day29", "Owner");
        UserView colleague = identity.createUser("colleague-" + suffix, "plain-secret-day29", "Colleague");
        TenantView other = identity.createTenant("qa-other-" + suffix, "Other " + suffix);
        TenantContext.set(other.id());
        UserView outsider = identity.createUser("outsider-" + suffix, "plain-secret-day29", "Outsider");

        UUID conversationId = store.insert(tenant.id(), owner.id());

        assertTrue(store.ownedBy(tenant.id(), owner.id(), conversationId));
        assertFalse(store.ownedBy(tenant.id(), colleague.id(), conversationId));
        assertFalse(store.ownedBy(other.id(), outsider.id(), conversationId));
        IdentityException missing = assertThrows(IdentityException.class, () -> {
            if (!store.ownedBy(tenant.id(), colleague.id(), conversationId)) {
                throw new IdentityException(org.springframework.http.HttpStatus.NOT_FOUND, "会话不存在");
            }
        });
        org.junit.jupiter.api.Assertions.assertEquals("会话不存在", missing.getMessage());
    }

    @Test
    void listsOwnerConversationsNewestFirstAndHidesOtherUsers() {
        String suffix = UUID.randomUUID().toString().substring(0, 8);
        TenantView tenant = identity.createTenant("qa-list-" + suffix, "QA " + suffix);
        TenantContext.set(tenant.id());
        UserView owner = identity.createUser("owner-" + suffix, "plain-secret-day29", "Owner");
        UserView colleague = identity.createUser("colleague-" + suffix, "plain-secret-day29", "Colleague");
        TenantView other = identity.createTenant("qa-list-other-" + suffix, "Other " + suffix);
        TenantContext.set(other.id());
        UserView outsider = identity.createUser("outsider-" + suffix, "plain-secret-day29", "Outsider");

        UUID outsiderConversation = store.insert(other.id(), outsider.id());
        UUID colleagueConversation = store.insert(tenant.id(), colleague.id());
        UUID older = UUID.randomUUID();
        UUID newer = UUID.randomUUID();
        insertAt(older, tenant.id(), owner.id(), Instant.parse("2026-01-01T00:00:00Z"));
        insertAt(newer, tenant.id(), owner.id(), Instant.parse("2026-01-02T00:00:00Z"));

        ConversationPage page = conversations.list(tenant.id(), owner.id(), 0, 10);
        assertEquals(List.of(newer, older), page.items().stream().map(ConversationSummary::conversationId).toList());
        assertEquals(2, page.total());
        assertEquals(List.of(newer), conversations.list(tenant.id(), owner.id(), 0, 1).items().stream()
                .map(ConversationSummary::conversationId).toList());
        assertEquals(List.of(older), conversations.list(tenant.id(), owner.id(), 1, 1).items().stream()
                .map(ConversationSummary::conversationId).toList());
        assertEquals(List.of(colleagueConversation), conversations.list(tenant.id(), colleague.id(), 0, 10).items().stream()
                .map(ConversationSummary::conversationId).toList());
        assertEquals(List.of(outsiderConversation), conversations.list(other.id(), outsider.id(), 0, 10).items().stream()
                .map(ConversationSummary::conversationId).toList());
        assertFalse(page.items().stream().anyMatch(item -> item.conversationId().equals(colleagueConversation)));
        assertFalse(page.items().stream().anyMatch(item -> item.conversationId().equals(outsiderConversation)));
    }

    private void insertAt(UUID id, UUID tenantId, UUID ownerUserId, Instant createdAt) {
        jdbc.update("""
                insert into business.agent_conversation (id, tenant_id, owner_user_id, created_at)
                values (?, ?, ?, ?)
                """, id, tenantId, ownerUserId, Timestamp.from(createdAt));
    }
}
