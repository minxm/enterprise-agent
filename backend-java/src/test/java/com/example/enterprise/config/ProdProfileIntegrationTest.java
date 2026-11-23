package com.example.enterprise.config;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.example.enterprise.agentgateway.MemoryController;
import com.example.enterprise.identity.IdentityService;
import com.example.enterprise.identity.IdentityRepository;
import com.example.enterprise.identity.IdentityViews.RoleView;
import com.example.enterprise.identity.IdentityViews.TenantView;
import com.example.enterprise.identity.IdentityViews.UserView;
import com.example.enterprise.identity.TenantContext;
import java.sql.SQLException;
import java.util.UUID;
import javax.sql.DataSource;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.ApplicationContext;
import org.springframework.http.MediaType;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;

@SpringBootTest
@ActiveProfiles("prod")
@AutoConfigureMockMvc
@Transactional
@EnabledIfEnvironmentVariable(named = "BUSINESS_APP_PASSWORD", matches = ".+")
@TestPropertySource(properties = {
        "AGENT_INTERNAL_TOKEN=",
        "agent.service.internal-token=",
        "MINIO_ENDPOINT=http://127.0.0.1:9000",
        "MINIO_ACCESS_KEY=prod-profile-test-access",
        "MINIO_SECRET_KEY=prod-profile-test-secret",
        "AGENT_SERVICE_BASE_URL=http://127.0.0.1:8000",
        "BUSINESS_MIGRATOR_PASSWORD="
})
class ProdProfileIntegrationTest {

    private static final String PASSWORD = "prod-profile-check";

    @Autowired
    private ApplicationContext context;

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private IdentityService identity;

    @Autowired
    private IdentityRepository repository;

    @Autowired
    private DataSource dataSource;

    @Autowired
    private SchemaCompatibility schemaCompatibility;

    @Test
    void prodUsesBusinessRolesAndRealJwtBoundaries() throws Exception {
        assertNotNull(context.getBean(MemoryController.class));
        assertTrue(context.getBeansOfType(Flyway.class).isEmpty());
        assertTrue(schemaCompatibility.versions().containsAll(BusinessSchemaGuard.expectedVersions()));
        try (var connection = dataSource.getConnection();
             var statement = connection.createStatement()) {
            try (var user = statement.executeQuery(
                    "select current_user, (select rolsuper from pg_roles where rolname = current_user)")) {
                user.next();
                assertEquals("business_app", user.getString(1));
                assertFalse(user.getBoolean(2));
            }
            try (var schema = statement.executeQuery(
                    "select has_schema_privilege(current_user, 'agent', 'USAGE')")) {
                schema.next();
                assertFalse(schema.getBoolean(1));
            }
            connection.setAutoCommit(false);
            try {
                statement.execute("create table business.day47_ddl_probe(id int)");
                throw new AssertionError("business_app executed DDL");
            } catch (SQLException ex) {
                assertEquals("42501", ex.getSQLState());
            } finally {
                connection.rollback();
            }
        }

        String suffix = UUID.randomUUID().toString().substring(0, 8);
        TenantView first = identity.createTenant("p45a-" + suffix, "Prod A");
        TenantContext.set(first.id());
        UserView admin = identity.createUser("admin", PASSWORD, "Admin");
        UserView member = identity.createUser("member", PASSWORD, "Member");
        identity.assignRole(admin.id(), role("tenant_admin"));
        identity.assignRole(member.id(), role("contract_manager"));

        TenantView second = identity.createTenant("p45b-" + suffix, "Prod B");
        TenantContext.set(second.id());
        UserView otherAdmin = identity.createUser("admin", PASSWORD, "Other");
        identity.assignRole(otherAdmin.id(), role("tenant_admin"));

        String adminToken = login("p45a-" + suffix, "admin");
        String otherToken = login("p45b-" + suffix, "admin");
        String memberToken = login("p45a-" + suffix, "member");

        String contractId = mockMvc.perform(post("/api/v1/contracts")
                        .header("Authorization", "Bearer " + adminToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {"contractNo":"P45-%s","title":"验收","contractType":"service","counterpartyName":"北辰","amount":10,"currency":"cny"}
                                """.formatted(suffix)))
                .andExpect(status().isOk())
                .andReturn()
                .getResponse()
                .getContentAsString();
        String id = com.jayway.jsonpath.JsonPath.read(contractId, "$.data.id");

        mockMvc.perform(get("/api/v1/contracts/" + id).header("Authorization", "Bearer " + adminToken))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.contractNo").value("P45-" + suffix));
        mockMvc.perform(get("/api/v1/contracts/" + id).header("Authorization", "Bearer " + otherToken))
                .andExpect(status().isNotFound());

        mockMvc.perform(get("/api/v1/agent/memories").header("Authorization", "Bearer not-a-jwt"))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.message").value("未认证"));
        mockMvc.perform(post("/api/v1/agent/memories")
                        .header("Authorization", "Bearer " + memberToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {"scope":"tenant","memoryType":"organization_practice","content":"租户口径"}
                                """))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.message").value("无权管理租户记忆"));
        mockMvc.perform(post("/api/v1/agent/memories")
                        .header("Authorization", "Bearer " + adminToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {"scope":"user","memoryType":"preference","content":"prod-check"}
                                """))
                .andExpect(status().isServiceUnavailable())
                .andExpect(jsonPath("$.message").value("Agent 服务认证未配置"));
    }

    private UUID role(String code) {
        return repository.findRoleByCode(code).map(RoleView::id).orElseThrow();
    }

    private String login(String tenantCode, String username) throws Exception {
        String body = mockMvc.perform(post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {"tenantCode":"%s","username":"%s","password":"%s"}
                                """.formatted(tenantCode, username, PASSWORD)))
                .andExpect(status().isOk())
                .andReturn()
                .getResponse()
                .getContentAsString();
        return com.jayway.jsonpath.JsonPath.read(body, "$.data.token");
    }
}
