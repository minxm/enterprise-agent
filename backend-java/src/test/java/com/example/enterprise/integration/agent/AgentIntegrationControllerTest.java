package com.example.enterprise.integration.agent;

import static org.hamcrest.Matchers.nullValue;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.example.enterprise.common.response.GlobalExceptionHandler;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.context.annotation.Import;
import org.springframework.http.HttpStatus;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

@WebMvcTest(AgentIntegrationController.class)
@Import(GlobalExceptionHandler.class)
class AgentIntegrationControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @MockitoBean
    private AgentClient agentClient;

    @Test
    void returnsUnifiedSuccessResponse() throws Exception {
        when(agentClient.health()).thenReturn(new AgentHealthResponse("ok", "agent-runtime"));

        mockMvc.perform(get("/api/v1/integration/agent/health"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.code").value(200))
                .andExpect(jsonPath("$.message").value("success"))
                .andExpect(jsonPath("$.data.status").value("ok"))
                .andExpect(jsonPath("$.data.service").value("agent-runtime"));
    }

    @Test
    void returnsDependencyErrorWhenPythonIsUnavailable() throws Exception {
        when(agentClient.health()).thenThrow(
                new AgentServiceException(HttpStatus.SERVICE_UNAVAILABLE, "Agent 服务不可用"));

        mockMvc.perform(get("/api/v1/integration/agent/health"))
                .andExpect(status().isServiceUnavailable())
                .andExpect(jsonPath("$.code").value(503))
                .andExpect(jsonPath("$.message").value("Agent 服务不可用"))
                .andExpect(jsonPath("$.data").value(nullValue()));
    }
}
