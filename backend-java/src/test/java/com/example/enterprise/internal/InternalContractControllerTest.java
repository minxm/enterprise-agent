package com.example.enterprise.internal;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.example.enterprise.auth.AuthenticatedUser;
import com.example.enterprise.config.RestClientConfig;
import com.example.enterprise.config.SecurityConfig;
import com.example.enterprise.contract.ContractExceptionHandler;
import com.example.enterprise.contract.ContractService;
import com.example.enterprise.contract.ContractViews.AgentContractSnapshot;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.context.annotation.Import;
import org.springframework.http.MediaType;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(InternalContractController.class)
@ActiveProfiles("dev")
@Import({ContractExceptionHandler.class, SecurityConfig.class, ActorTicketStore.class, RestClientConfig.class})
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
class InternalContractControllerTest {

    private static final UUID TENANT = UUID.fromString("00000000-0000-4000-8000-00000000e331");
    private static final UUID ACTOR = UUID.fromString("00000000-0000-4000-8000-00000000a331");

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private ActorTicketStore tickets;

    @MockitoBean
    private ContractService contracts;

    @Test
    void missingOrWrongTokenIsRejectedBeforeTheContractService() throws Exception {
        mockMvc.perform(get("/internal/v1/contracts").param("contractNo", "HT-2026-001"))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.message").value("未认证"));
        mockMvc.perform(get("/internal/v1/contracts")
                        .param("contractNo", "HT-2026-001")
                        .header("X-Agent-Token", "wrong-token")
                        .header("X-Actor-Ticket", tickets.issue(TENANT, ACTOR)))
                .andExpect(status().isUnauthorized());
        verifyNoInteractions(contracts);
    }

    @Test
    void missingTicketIsRejected() throws Exception {
        mockMvc.perform(get("/internal/v1/contracts")
                        .param("contractNo", "HT-2026-001")
                        .header("X-Agent-Token", "test-internal-token"))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.message").value("未认证"));
        verifyNoInteractions(contracts);
    }

    @Test
    void identityParametersAreRejected() throws Exception {
        mockMvc.perform(get("/internal/v1/contracts")
                        .param("contractNo", "HT-2026-001")
                        .param("tenantId", TENANT.toString())
                        .header("X-Agent-Token", "test-internal-token")
                        .header("X-Actor-Ticket", tickets.issue(TENANT, ACTOR)))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.message").value("请求参数不合法"));
        verifyNoInteractions(contracts);
    }

    @Test
    void usesTheTicketIdentityAndIgnoresClientHeaders() throws Exception {
        when(contracts.queryForAgent(any(), eq("HT-2026-001"))).thenReturn(sample());

        mockMvc.perform(get("/internal/v1/contracts")
                        .param("contractNo", "HT-2026-001")
                        .header("X-Agent-Token", "test-internal-token")
                        .header("X-Actor-Ticket", tickets.issue(TENANT, ACTOR))
                        .header("X-Tenant-Id", "00000000-0000-4000-8000-00000000ffff")
                        .header("X-User-Id", "00000000-0000-4000-8000-00000000eeee"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.contractNo").value("HT-2026-001"))
                .andExpect(jsonPath("$.data.statusLabel").value("待审核"))
                .andExpect(jsonPath("$.data.currentVersion.versionLabel").value("V1"))
                .andExpect(jsonPath("$.data.objectKey").doesNotExist());

        verify(contracts).queryForAgent(
                eq(new AuthenticatedUser(ACTOR, TENANT, "")),
                eq("HT-2026-001"));
    }

    @Test
    void aSpentTicketCannotQueryAgain() throws Exception {
        when(contracts.queryForAgent(any(), eq("HT-2026-001"))).thenReturn(sample());
        String ticket = tickets.issue(TENANT, ACTOR);

        mockMvc.perform(get("/internal/v1/contracts")
                        .param("contractNo", "HT-2026-001")
                        .header("X-Agent-Token", "test-internal-token")
                        .header("X-Actor-Ticket", ticket))
                .andExpect(status().isOk());
        mockMvc.perform(get("/internal/v1/contracts")
                        .param("contractNo", "HT-2026-009")
                        .header("X-Agent-Token", "test-internal-token")
                        .header("X-Actor-Ticket", ticket)
                        .header("X-Tenant-Id", "00000000-0000-4000-8000-00000000ffff")
                        .header("X-User-Id", "00000000-0000-4000-8000-00000000eeee"))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.message").value("未认证"));

        verify(contracts, times(1)).queryForAgent(any(), eq("HT-2026-001"));
    }

    @Test
    void writesAreNotExposed() throws Exception {
        mockMvc.perform(post("/internal/v1/contracts")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"title\":\"x\"}")
                        .header("X-Agent-Token", "test-internal-token"))
                .andExpect(status().isInternalServerError())
                .andExpect(jsonPath("$.message").value("服务内部错误"));
        verifyNoInteractions(contracts);
    }

    private static AgentContractSnapshot sample() {
        return new AgentContractSnapshot(
                "HT-2026-001",
                "年度采购合同",
                "采购",
                "南方公司",
                "1.00",
                "CNY",
                null,
                null,
                null,
                "pending_review",
                "待审核",
                null,
                new com.example.enterprise.contract.ContractViews.AgentContractVersion(
                        1, "V1", "首版", "采购合同.pdf", java.time.Instant.parse("2026-11-19T02:00:00Z")));
    }
}
