package com.example.enterprise.auth;

import com.example.enterprise.identity.IdentityException;
import com.example.enterprise.identity.IdentityRepository;
import com.example.enterprise.identity.IdentityService;
import com.example.enterprise.identity.IdentityViews.TenantView;
import com.example.enterprise.identity.IdentityViews.UserView;
import com.example.enterprise.identity.TenantContext;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest
@ActiveProfiles("dev")
@AutoConfigureMockMvc
@Transactional
@EnabledIfEnvironmentVariable(named = "BUSINESS_APP_PASSWORD", matches = ".+")
class AuthIntegrationTest {

    private static final String PASSWORD = "plain-secret-day11";

    @Autowired
    private IdentityService identity;

    @Autowired
    private IdentityRepository repository;

    @Autowired
    private JwtService tokens;

    @Autowired
    private MockMvc mockMvc;

    @AfterEach
    void clearContext() {
        TenantContext.clear();
        SecurityContextHolder.clearContext();
    }

    @Test
    void loginSucceedsForActiveUserAndTenant() throws Exception {
        Created created = user("acme-login", "ada", PASSWORD);

        String token = tokenOf(loginRaw("acme-login", "ada", PASSWORD, 200));

        mockMvc.perform(get("/api/v1/identity/tenant").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.id").value(created.tenant().id().toString()))
                .andExpect(jsonPath("$.data.code").value("acme-login"));
        assertThrows(IdentityException.class, TenantContext::require);
    }

    @Test
    void wrongPasswordIsRejected() throws Exception {
        user("acme-bad-password", "ada", PASSWORD);

        loginRaw("acme-bad-password", "ada", "wrong-password", 401);
    }

    @Test
    void missingUserIsRejectedWithTheSameMessage() throws Exception {
        user("acme-missing", "ada", PASSWORD);

        String wrongPassword = loginRaw("acme-missing", "ada", "wrong-password", 401);
        String missingUser = loginRaw("acme-missing", "nobody", PASSWORD, 401);
        String wrongMessage = com.jayway.jsonpath.JsonPath.read(wrongPassword, "$.message");
        String missingMessage = com.jayway.jsonpath.JsonPath.read(missingUser, "$.message");
        assertEquals("登录失败", wrongMessage);
        assertEquals(wrongMessage, missingMessage);
    }

    @Test
    void disabledUserCannotLogin() throws Exception {
        Created created = user("acme-user-off", "ada", PASSWORD);
        repository.applyTenant(created.tenant().id());
        repository.updateUserStatus(created.user().id(), "disabled");

        loginRaw("acme-user-off", "ada", PASSWORD, 401);
    }

    @Test
    void disabledTenantCannotLogin() throws Exception {
        Created created = user("acme-tenant-off", "ada", PASSWORD);
        repository.updateTenantStatus(created.tenant().id(), "disabled");

        loginRaw("acme-tenant-off", "ada", PASSWORD, 401);
    }

    @Test
    void loginResponseDoesNotExposePasswordMaterial() throws Exception {
        Created created = user("acme-no-secret", "ada", PASSWORD);
        repository.applyTenant(created.tenant().id());
        String hash = repository.findPasswordHash(created.user().id()).orElseThrow();

        String body = loginRaw("acme-no-secret", "ada", PASSWORD, 200);

        assertFalse(body.contains(PASSWORD));
        assertFalse(body.contains(hash));
        assertFalse(body.contains("password"));
        assertEquals(created.tenant().id().toString(), com.jayway.jsonpath.JsonPath.read(body, "$.data.tenant.id"));
        assertEquals("ada", com.jayway.jsonpath.JsonPath.read(body, "$.data.user.loginName"));
    }

    @Test
    void sameLoginNameStaysInsideItsTenant() throws Exception {
        user("acme-a", "ada", "password-a");
        user("acme-b", "ada", "password-b");

        loginRaw("acme-a", "ada", "password-b", 401);
        String body = loginRaw("acme-b", "ada", "password-b", 200);
        assertEquals("acme-b", com.jayway.jsonpath.JsonPath.read(body, "$.data.tenant.code"));
    }

    @Test
    void protectedApiRejectsMissingAndInvalidTokens() throws Exception {
        Created created = user("acme-filter", "ada", PASSWORD);
        String token = tokenOf(loginRaw("acme-filter", "ada", PASSWORD, 200));

        mockMvc.perform(get("/api/v1/identity/tenant"))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.message").value("未认证"))
                .andExpect(header().exists("X-Request-ID"));

        mockMvc.perform(get("/api/v1/identity/tenant").header("Authorization", "Bearer not-a-jwt"))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.message").value("未认证"));

        String expired = tokens.issue(
                created.user().id(),
                created.tenant().id(),
                Instant.now().minus(Duration.ofHours(3)),
                Instant.now().minus(Duration.ofHours(1)));
        mockMvc.perform(get("/api/v1/identity/tenant").header("Authorization", "Bearer " + expired))
                .andExpect(status().isUnauthorized());

        JwtService other = new JwtService(
                "fedcba9876543210fedcba9876543210",
                "enterprise-agent",
                Duration.ofHours(2),
                Clock.systemUTC());
        String forged = other.issue(created.user().id(), created.tenant().id());
        mockMvc.perform(get("/api/v1/identity/tenant").header("Authorization", "Bearer " + forged))
                .andExpect(status().isUnauthorized());

        mockMvc.perform(get("/api/v1/identity/tenant").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.id").value(created.tenant().id().toString()));
    }

    @Test
    void disabledUserRejectsAnExistingToken() throws Exception {
        Created created = user("acme-old-user", "ada", PASSWORD);
        String token = tokenOf(loginRaw("acme-old-user", "ada", PASSWORD, 200));
        repository.applyTenant(created.tenant().id());
        repository.updateUserStatus(created.user().id(), "disabled");

        mockMvc.perform(get("/api/v1/identity/tenant").header("Authorization", "Bearer " + token))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.message").value("未认证"));
    }

    @Test
    void disabledTenantRejectsAnExistingToken() throws Exception {
        Created created = user("acme-old-tenant", "ada", PASSWORD);
        String token = tokenOf(loginRaw("acme-old-tenant", "ada", PASSWORD, 200));
        repository.updateTenantStatus(created.tenant().id(), "disabled");

        mockMvc.perform(get("/api/v1/identity/tenant").header("Authorization", "Bearer " + token))
                .andExpect(status().isUnauthorized());
    }

    @Test
    void clientTenantIdDoesNotReplaceTheTokenTenant() throws Exception {
        Created first = user("acme-ctx-a", "ada", PASSWORD);
        Created second = user("acme-ctx-b", "ada", "other-secret-day11");
        String token = tokenOf(loginRaw("acme-ctx-a", "ada", PASSWORD, 200));

        mockMvc.perform(get("/api/v1/identity/tenant")
                        .header("Authorization", "Bearer " + token)
                        .header("X-Tenant-Id", second.tenant().id().toString())
                        .param("tenantId", second.tenant().id().toString()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.id").value(first.tenant().id().toString()));
        assertThrows(IdentityException.class, TenantContext::require);

        String other = tokenOf(loginRaw("acme-ctx-b", "ada", "other-secret-day11", 200));
        mockMvc.perform(get("/api/v1/identity/tenant").header("Authorization", "Bearer " + other))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.id").value(second.tenant().id().toString()));
        assertThrows(IdentityException.class, TenantContext::require);
    }

    private Created user(String tenantCode, String username, String password) {
        TenantView tenant = identity.createTenant(tenantCode, tenantCode);
        TenantContext.set(tenant.id());
        UserView createdUser = identity.createUser(username, password, username);
        TenantContext.clear();
        return new Created(tenant, createdUser);
    }

    private String loginRaw(String tenantCode, String username, String password, int status) throws Exception {
        return mockMvc.perform(post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"tenantCode\":\"" + tenantCode
                                + "\",\"username\":\"" + username
                                + "\",\"password\":\"" + password + "\"}"))
                .andExpect(status().is(status))
                .andExpect(jsonPath("$.message").value(status == 200 ? "success" : "登录失败"))
                .andReturn()
                .getResponse()
                .getContentAsString();
    }

    private String tokenOf(String body) {
        return com.jayway.jsonpath.JsonPath.read(body, "$.data.token");
    }

    private record Created(TenantView tenant, UserView user) {
    }
}
