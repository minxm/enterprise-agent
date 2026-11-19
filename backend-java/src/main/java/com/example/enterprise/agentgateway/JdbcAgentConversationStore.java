package com.example.enterprise.agentgateway;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.UUID;
import org.springframework.context.annotation.Profile;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.annotation.Transactional;

@Repository
@Profile("dev")
public class JdbcAgentConversationStore implements AgentConversationStore {

    private final JdbcTemplate jdbc;

    public JdbcAgentConversationStore(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @Override
    @Transactional
    public UUID insert(UUID tenantId, UUID ownerUserId) {
        applyTenant(tenantId);
        UUID id = UUID.randomUUID();
        jdbc.update("""
                insert into business.agent_conversation (id, tenant_id, owner_user_id)
                values (?, ?, ?)
                """, id, tenantId, ownerUserId);
        return id;
    }

    @Override
    @Transactional
    public boolean ownedBy(UUID tenantId, UUID ownerUserId, UUID conversationId) {
        applyTenant(tenantId);
        Integer count = jdbc.queryForObject("""
                select count(*)
                from business.agent_conversation
                where id = ? and tenant_id = ? and owner_user_id = ?
                """, Integer.class, conversationId, tenantId, ownerUserId);
        return count != null && count == 1;
    }

    @Override
    @Transactional
    public List<ConversationSummary> list(UUID tenantId, UUID ownerUserId, int limit, long offset) {
        applyTenant(tenantId);
        // RLS only isolates tenants. The owner predicate keeps another user in the same tenant out.
        return jdbc.query("""
                select id, created_at
                from business.agent_conversation
                where tenant_id = ? and owner_user_id = ?
                order by created_at desc, id desc
                limit ? offset ?
                """, (rs, rowNum) -> new ConversationSummary(
                rs.getObject("id", UUID.class),
                rs.getObject("created_at", OffsetDateTime.class).toInstant()),
                tenantId, ownerUserId, limit, offset);
    }

    @Override
    @Transactional
    public long count(UUID tenantId, UUID ownerUserId) {
        applyTenant(tenantId);
        Long count = jdbc.queryForObject("""
                select count(*)
                from business.agent_conversation
                where tenant_id = ? and owner_user_id = ?
                """, Long.class, tenantId, ownerUserId);
        return count == null ? 0 : count;
    }

    private void applyTenant(UUID tenantId) {
        jdbc.queryForObject("select set_config('app.tenant_id', ?, true)", String.class, tenantId.toString());
    }
}
