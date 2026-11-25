package com.example.enterprise.internal;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.example.enterprise.config.RestClientConfig;
import com.example.enterprise.config.SecurityConfig;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(InternalKnowledgeQaTicketController.class)
@ActiveProfiles("dev")
@Import({
        SecurityConfig.class,
        RestClientConfig.class,
        InternalKnowledgeQaTicketControllerTest.MemoryTickets.class
})
@TestPropertySource(properties = {
        "agent.service.base-url=http://127.0.0.1:9",
        "agent.service.health-path=/api/v1/health",
        "agent.service.internal-token=test-internal-token",
        "JWT_SECRET=test-jwt-secret-at-least-32-characters",
        "POSTGRES_HOST=127.0.0.1",
        "POSTGRES_PORT=5432",
        "POSTGRES_DB=enterprise",
        "BUSINESS_APP_PASSWORD=unused",
        "BUSINESS_MIGRATOR_PASSWORD=unused",
        "REDIS_HOST=127.0.0.1",
        "REDIS_PORT=6379",
        "REDIS_PASSWORD=unused"
})
class InternalKnowledgeQaTicketControllerTest {

    private static final UUID TENANT = UUID.fromString("00000000-0000-4000-8000-00000000e360");
    private static final UUID ACTOR = UUID.fromString("00000000-0000-4000-8000-00000000a360");
    private static final UUID CONVERSATION = UUID.fromString("00000000-0000-4000-8000-00000000c360");

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private KnowledgeQaTicketStore tickets;

    @Test
    void missingOrWrongTokenDoesNotConsumeTheTicket() throws Exception {
        String ticket = tickets.issue(TENANT, ACTOR, CONVERSATION, KnowledgeQaTicketStore.ACTION_STREAM, false);
        mockMvc.perform(get("/internal/v1/knowledge-qa/tickets/redeem")
                        .header("X-Knowledge-Qa-Ticket", ticket))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.message").value("未认证"));
        mockMvc.perform(get("/internal/v1/knowledge-qa/tickets/redeem")
                        .header("X-Agent-Token", "wrong-token")
                        .header("X-Knowledge-Qa-Ticket", ticket))
                .andExpect(status().isUnauthorized());
        assertTrue(tickets.consume(ticket).isPresent());
    }

    @Test
    void queryParametersAreRejectedBeforeConsumption() throws Exception {
        String ticket = tickets.issue(TENANT, ACTOR, CONVERSATION, KnowledgeQaTicketStore.ACTION_HISTORY, true);
        mockMvc.perform(get("/internal/v1/knowledge-qa/tickets/redeem")
                        .param("tenant_id", TENANT.toString())
                        .header("X-Agent-Token", "test-internal-token")
                        .header("X-Knowledge-Qa-Ticket", ticket))
                .andExpect(status().isBadRequest());
        assertEquals(ACTOR, tickets.consume(ticket).orElseThrow().actorId());
    }

    @Test
    void redeemReturnsTheStoredClaimAndASecondCallFails() throws Exception {
        String ticket = tickets.issue(TENANT, ACTOR, CONVERSATION, KnowledgeQaTicketStore.ACTION_STREAM, true);
        String body = mockMvc.perform(get("/internal/v1/knowledge-qa/tickets/redeem")
                        .header("X-Agent-Token", "test-internal-token")
                        .header("X-Knowledge-Qa-Ticket", ticket))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.tenantId").value(TENANT.toString()))
                .andExpect(jsonPath("$.data.actorId").value(ACTOR.toString()))
                .andExpect(jsonPath("$.data.conversationId").value(CONVERSATION.toString()))
                .andExpect(jsonPath("$.data.action").value(KnowledgeQaTicketStore.ACTION_STREAM))
                .andExpect(jsonPath("$.data.allowTenantRead").value(true))
                .andReturn()
                .getResponse()
                .getContentAsString();
        assertFalse(body.contains(ticket));
        mockMvc.perform(get("/internal/v1/knowledge-qa/tickets/redeem")
                        .header("X-Agent-Token", "test-internal-token")
                        .header("X-Knowledge-Qa-Ticket", ticket))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.message").value("未认证"));
    }

    @TestConfiguration
    static class MemoryTickets {
        @Bean
        KnowledgeQaTicketStore knowledgeQaTicketStore() {
            return KnowledgeQaTicketStores.memory();
        }
    }
}
