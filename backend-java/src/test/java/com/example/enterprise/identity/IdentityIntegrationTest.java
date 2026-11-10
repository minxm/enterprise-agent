package com.example.enterprise.identity;

import com.example.enterprise.identity.IdentityViews.RoleView;
import com.example.enterprise.identity.IdentityViews.TenantView;
import com.example.enterprise.identity.IdentityViews.UserView;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.transaction.support.TransactionTemplate;

import java.util.UUID;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest
@ActiveProfiles("dev")
@AutoConfigureMockMvc
@Transactional
@EnabledIfEnvironmentVariable(named = "BUSINESS_APP_PASSWORD", matches = ".+")
class IdentityIntegrationTest {

    private static final String RAW_PASSWORD = "plain-secret-day10";

    @Autowired
    private IdentityService identity;

    @Autowired
    private IdentityRepository repository;

    @Autowired
    private PasswordHasher passwords;

    @Autowired
    private JdbcTemplate jdbc;

    @Autowired
    private PlatformTransactionManager transactionManager;

    @Autowired
    private MockMvc mockMvc;

    @AfterEach
    void clearTenant() {
        TenantContext.clear();
        SecurityContextHolder.clearContext();
    }

    @Test
    void flywayCreatesTheSixIdentityTables() {
        Integer tables = jdbc.queryForObject("""
                select count(*)
                from information_schema.tables
                where table_schema = 'business'
                  and table_name in ('tenant', 'app_user', 'role', 'permission', 'user_role', 'role_permission')
                """, Integer.class);
        Integer constraints = jdbc.queryForObject("""
                select count(*)
                from pg_constraint
                where conname in (
                    'tenant_code_unique', 'app_user_login_unique', 'role_code_unique',
                    'user_role_pkey', 'role_permission_pkey',
                    'user_role_user_fk', 'user_role_role_fk', 'role_permission_role_fk')
                """, Integer.class);
        Boolean migrated = jdbc.queryForObject("""
                select success from business.flyway_schema_history where version = '1'
                """, Boolean.class);
        Integer indexes = jdbc.queryForObject("""
                select count(*)
                from pg_indexes
                where schemaname = 'business'
                  and indexname in (
                    'app_user_tenant_idx', 'role_tenant_idx',
                    'user_role_tenant_idx', 'role_permission_tenant_idx')
                """, Integer.class);
        Integer permissions = jdbc.queryForObject("select count(*) from business.permission", Integer.class);
        assertEquals(6, tables);
        assertEquals(8, constraints);
        assertEquals(4, indexes);
        assertEquals(Boolean.TRUE, migrated);
        assertEquals(6, permissions);
    }

    @Test
    void tenantUserRoleAndPermissionAssociationsMatchTheMatrix() {
        TenantView tenant = identity.createTenant("acme", "Acme");
        TenantContext.set(tenant.id());
        UserView user = identity.createUser("ada", RAW_PASSWORD, "Ada");
        RoleView admin = repository.findRoleByCode("tenant_admin").orElseThrow();
        identity.assignRole(user.id(), admin.id());

        assertEquals("active", identity.currentTenant().status());
        assertEquals(1, identity.listUsers().size());
        assertEquals("tenant_admin", identity.listRoles(user.id()).getFirst().code());
        assertEquals(6, identity.listPermissions(admin.id()).size());
        assertTrue(identity.checkUserRole(user.id(), repository.findRoleByCode("approver").orElseThrow().id()).valid());
    }

    @Test
    void loginNameIsUniqueInsideATenant() {
        TenantView tenant = identity.createTenant("acme-login", "Acme");
        TenantContext.set(tenant.id());
        identity.createUser("ada", RAW_PASSWORD, "Ada");
        IdentityException exception = assertThrows(IdentityException.class,
                () -> identity.createUser("ada", RAW_PASSWORD, "Ada Two"));
        assertEquals("同一租户内登录名已存在", exception.getMessage());
    }

    @Test
    void roleCodeIsUniqueInsideATenant() {
        TenantView tenant = identity.createTenant("acme-role", "Acme");
        TenantContext.set(tenant.id());
        repository.applyTenant(tenant.id());
        IdentityException exception = assertThrows(IdentityException.class, () -> inSavepoint(() ->
                repository.insertRole(UUID.randomUUID(), tenant.id(), "tenant_admin", "重复管理员")));
        assertEquals("同一租户内角色编码已存在", exception.getMessage());
    }

    @Test
    void duplicateUserRoleIsRejected() {
        TenantView tenant = identity.createTenant("acme-link", "Acme");
        TenantContext.set(tenant.id());
        UserView user = identity.createUser("ada", RAW_PASSWORD, "Ada");
        UUID roleId = repository.findRoleByCode("approver").orElseThrow().id();
        identity.assignRole(user.id(), roleId);
        IdentityException exception = assertThrows(IdentityException.class, () -> identity.assignRole(user.id(), roleId));
        assertEquals("用户角色关联已存在", exception.getMessage());
    }

    @Test
    void duplicateRolePermissionIsRejected() {
        TenantView tenant = identity.createTenant("acme-grant", "Acme");
        TenantContext.set(tenant.id());
        repository.applyTenant(tenant.id());
        UUID roleId = repository.findRoleByCode("approver").orElseThrow().id();
        UUID permissionId = repository.findPermissionId("contract:read");
        IdentityException exception = assertThrows(IdentityException.class, () -> inSavepoint(() ->
                repository.insertRolePermission(roleId, permissionId, tenant.id())));
        assertEquals("角色权限关联已存在", exception.getMessage());
    }

    @Test
    void crossTenantRoleAssignmentIsRejected() {
        TenantView first = identity.createTenant("acme-a", "Acme A");
        TenantContext.set(first.id());
        UserView user = identity.createUser("ada", RAW_PASSWORD, "Ada");
        TenantView second = identity.createTenant("acme-b", "Acme B");
        TenantContext.set(second.id());
        repository.applyTenant(second.id());
        UUID foreignRole = repository.findRoleByCode("approver").orElseThrow().id();

        TenantContext.set(first.id());
        repository.applyTenant(first.id());
        assertFalse(identity.checkUserRole(user.id(), foreignRole).valid());
        assertEquals("用户或角色不存在", identity.checkUserRole(user.id(), foreignRole).reason());
        IdentityException exception = assertThrows(IdentityException.class, () -> inSavepoint(() ->
                repository.insertUserRole(user.id(), foreignRole, first.id())));
        assertEquals("不能跨租户分配角色", exception.getMessage());
    }

    @Test
    void passwordIsStoredAsHashAndIsNotReturned() throws Exception {
        TenantView tenant = identity.createTenant("acme-secret", "Acme");
        TenantContext.set(tenant.id());
        UserView user = identity.createUser("ada", RAW_PASSWORD, "Ada");
        String hash = repository.findPasswordHash(user.id()).orElseThrow();
        assertNotEquals(RAW_PASSWORD, hash);
        assertFalse(hash.contains(RAW_PASSWORD));
        assertTrue(hash.startsWith("$2"));
        assertTrue(passwords.matches(RAW_PASSWORD, hash));

        String token = login("acme-secret", "ada", RAW_PASSWORD);
        String body = mockMvc.perform(get("/api/v1/identity/users")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data[0].loginName").value("ada"))
                .andReturn()
                .getResponse()
                .getContentAsString();
        assertFalse(body.contains(RAW_PASSWORD));
        assertFalse(body.contains(hash));
        assertFalse(body.contains("password"));
    }

    @Test
    void requestTenantIdIsNotTrusted() throws Exception {
        TenantView tenant = identity.createTenant("acme-context", "Acme");
        mockMvc.perform(get("/api/v1/identity/tenant").param("tenantId", tenant.id().toString()))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.message").value("未认证"));
        assertThrows(IdentityException.class, TenantContext::require);

        TenantContext.set(tenant.id());
        identity.createUser("ada", RAW_PASSWORD, "Ada");
        String token = login("acme-context", "ada", RAW_PASSWORD);
        mockMvc.perform(get("/api/v1/identity/tenant")
                        .header("Authorization", "Bearer " + token)
                        .header("X-Tenant-Id", UUID.randomUUID().toString())
                        .param("tenantId", UUID.randomUUID().toString()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.id").value(tenant.id().toString()));
        assertThrows(IdentityException.class, TenantContext::require);

        mockMvc.perform(post("/api/v1/identity/user-roles/check")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"userId\":\"" + UUID.randomUUID()
                                + "\",\"roleId\":\"" + UUID.randomUUID()
                                + "\",\"tenantId\":\"" + UUID.randomUUID() + "\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.valid").value(false));
    }

    private String login(String tenantCode, String username, String password) throws Exception {
        String body = mockMvc.perform(post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"tenantCode\":\"" + tenantCode
                                + "\",\"username\":\"" + username
                                + "\",\"password\":\"" + password + "\"}"))
                .andExpect(status().isOk())
                .andReturn()
                .getResponse()
                .getContentAsString();
        return com.jayway.jsonpath.JsonPath.read(body, "$.data.token");
    }

    @Test
    void disabledTenantIsRecognized() {
        TenantView tenant = identity.createTenant("acme-off", "Acme");
        TenantContext.set(tenant.id());
        repository.updateTenantStatus(tenant.id(), "disabled");
        assertEquals("disabled", identity.currentTenant().status());
        IdentityException exception = assertThrows(IdentityException.class,
                () -> identity.createUser("ada", RAW_PASSWORD, "Ada"));
        assertEquals("租户已禁用", exception.getMessage());
    }

    private void inSavepoint(Runnable action) {
        TransactionTemplate nested = new TransactionTemplate(transactionManager);
        nested.setPropagationBehavior(TransactionDefinition.PROPAGATION_NESTED);
        nested.executeWithoutResult(status -> action.run());
    }
}
