package com.example.enterprise.config;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.example.enterprise.agentgateway.MemoryController;
import com.example.enterprise.auth.AuthController;
import com.example.enterprise.auth.JwtService;
import com.example.enterprise.identity.IdentityService;
import javax.sql.DataSource;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.ApplicationContext;
import org.springframework.test.web.servlet.MockMvc;

@SpringBootTest
@AutoConfigureMockMvc
class DefaultProfileBoundaryTest {

    @Autowired
    private ApplicationContext context;

    @Autowired
    private MockMvc mockMvc;

    @Test
    void defaultProfileDoesNotAssembleBusinessInfrastructure() {
        assertThat(context.getBeanProvider(DataSource.class).getIfAvailable()).isNull();
        assertThat(context.getBeanProvider(IdentityService.class).getIfAvailable()).isNull();
        assertThat(context.getBeanProvider(JwtService.class).getIfAvailable()).isNull();
        assertThat(context.getBeanProvider(AuthController.class).getIfAvailable()).isNull();
        assertThat(context.getBeanProvider(MemoryController.class).getIfAvailable()).isNull();
    }

    @Test
    void anonymousBusinessAndUnlistedRoutesAreRejected() throws Exception {
        mockMvc.perform(get("/api/v1/agent/memories"))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.message").value("未认证"));
        mockMvc.perform(get("/api/v1/contracts"))
                .andExpect(status().isUnauthorized());
        mockMvc.perform(get("/api/v1/test/ping"))
                .andExpect(status().isUnauthorized());
        mockMvc.perform(get("/api/v1/not-registered"))
                .andExpect(status().isUnauthorized());
    }

    @Test
    void onlyHealthAndInfoAreAnonymousActuatorRoutes() throws Exception {
        mockMvc.perform(get("/actuator/health"))
                .andExpect(status().isOk());
        mockMvc.perform(get("/actuator/env"))
                .andExpect(status().isForbidden());
    }
}
