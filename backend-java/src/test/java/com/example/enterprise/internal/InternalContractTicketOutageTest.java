package com.example.enterprise.internal;

import static org.mockito.Mockito.verifyNoInteractions;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.example.enterprise.common.response.GlobalExceptionHandler;
import com.example.enterprise.config.RestClientConfig;
import com.example.enterprise.config.SecurityConfig;
import com.example.enterprise.contract.ContractExceptionHandler;
import com.example.enterprise.contract.ContractService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(InternalContractController.class)
@ActiveProfiles("dev")
@Import({
        ContractExceptionHandler.class,
        GlobalExceptionHandler.class,
        SecurityConfig.class,
        RestClientConfig.class,
        InternalContractTicketOutageTest.FailingTickets.class
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
class InternalContractTicketOutageTest {

    @Autowired
    private MockMvc mockMvc;

    @MockitoBean
    private ContractService contracts;

    @Test
    void redisOutageIsServiceUnavailableAfterTheTokenCheck() throws Exception {
        mockMvc.perform(get("/internal/v1/contracts")
                        .param("contractNo", "HT-2026-001")
                        .header("X-Agent-Token", "wrong-token")
                        .header("X-Actor-Ticket", "still-valid"))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.message").value("未认证"));
        mockMvc.perform(get("/internal/v1/contracts")
                        .param("contractNo", "HT-2026-001")
                        .header("X-Agent-Token", "test-internal-token")
                        .header("X-Actor-Ticket", "still-valid"))
                .andExpect(status().isServiceUnavailable())
                .andExpect(jsonPath("$.message").value("Ticket 存储不可用"));
        verifyNoInteractions(contracts);
    }

    @TestConfiguration
    static class FailingTickets {
        @Bean
        ActorTicketStore actorTicketStore() {
            return ActorTicketStores.failing();
        }
    }
}
