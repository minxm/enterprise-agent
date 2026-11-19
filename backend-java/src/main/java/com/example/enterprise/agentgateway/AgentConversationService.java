package com.example.enterprise.agentgateway;

import com.example.enterprise.identity.IdentityException;
import java.util.UUID;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class AgentConversationService {

    private final ObjectProvider<AgentConversationStore> stores;

    public AgentConversationService(ObjectProvider<AgentConversationStore> stores) {
        this.stores = stores;
    }

    public UUID create(UUID tenantId, UUID ownerUserId) {
        return store().insert(tenantId, ownerUserId);
    }

    @Transactional
    public ConversationPage list(UUID tenantId, UUID ownerUserId, int page, int size) {
        if (page < 0 || size < 1 || size > 100) {
            throw new IdentityException(HttpStatus.BAD_REQUEST, "请求参数不合法");
        }
        AgentConversationStore store = store();
        return new ConversationPage(
                store.list(tenantId, ownerUserId, size, (long) page * size),
                page,
                size,
                store.count(tenantId, ownerUserId));
    }

    public void requireOwned(UUID tenantId, UUID ownerUserId, UUID conversationId) {
        if (!store().ownedBy(tenantId, ownerUserId, conversationId)) {
            throw new IdentityException(HttpStatus.NOT_FOUND, "会话不存在");
        }
    }

    private AgentConversationStore store() {
        AgentConversationStore store = stores.getIfAvailable();
        if (store == null) {
            throw new IdentityException(HttpStatus.SERVICE_UNAVAILABLE, "会话功能未启用");
        }
        return store;
    }
}
