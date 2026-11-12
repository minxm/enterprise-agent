package com.example.enterprise.contract;

import com.example.enterprise.file.ObjectStorage;
import com.example.enterprise.file.StorageProperties;
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
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.mock.web.MockMultipartFile;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionTemplate;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.multipart;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.request;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest
@ActiveProfiles("dev")
@AutoConfigureMockMvc
@Transactional
@EnabledIfEnvironmentVariable(named = "MINIO_ROOT_PASSWORD", matches = ".+")
class ContractVersionIntegrationTest {

    private static final String PASSWORD = "plain-secret-day14";
    private static final byte[] PDF = "%PDF-1.4\nversion\n%%EOF\n".getBytes(StandardCharsets.US_ASCII);

    @Autowired
    private IdentityService identity;

    @Autowired
    private JdbcTemplate jdbc;

    @Autowired
    private ObjectStorage storage;

    @Autowired
    private StorageProperties properties;

    @Autowired
    private PlatformTransactionManager transactionManager;

    @Autowired
    private MockMvc mockMvc;

    private final List<String> objects = new ArrayList<>();

    @AfterEach
    void cleanup() {
        for (String objectKey : objects) {
            storage.delete(properties.getBucket(), objectKey);
        }
        TenantContext.clear();
        SecurityContextHolder.clearContext();
    }

    @Test
    void migrationCreatesVersionTableAndDatabaseConstraints() {
        Boolean migrated = jdbc.queryForObject("""
                select success from business.flyway_schema_history where version = '4'
                """, Boolean.class);
        Integer forced = jdbc.queryForObject("""
                select count(*)
                from pg_class c
                join pg_namespace n on n.oid = c.relnamespace
                where n.nspname = 'business' and c.relname = 'contract_version'
                  and c.relrowsecurity and c.relforcerowsecurity
                """, Integer.class);
        String currentIndex = jdbc.queryForObject("""
                select indexdef from pg_indexes
                where schemaname = 'business' and indexname = 'contract_version_current_unique'
                """, String.class);
        Integer named = jdbc.queryForObject("""
                select count(*) from pg_constraint
                where conname in (
                    'contract_version_no_unique', 'contract_version_contract_fk',
                    'contract_version_file_fk', 'contract_version_creator_fk')
                """, Integer.class);
        assertEquals(Boolean.TRUE, migrated);
        assertEquals(1, forced);
        assertEquals(4, named);
        assertTrue(currentIndex.contains("UNIQUE"));
        assertTrue(currentIndex.toLowerCase().contains("where"));
        assertTrue(currentIndex.contains("is_current"));

        Account owner = account("version-db", "ada");
        Account other = account("version-db-b", "ada");
        UUID contractId = UUID.randomUUID();
        jdbc.queryForObject("select set_config('app.tenant_id', ?, true)", String.class, owner.tenant().id().toString());
        jdbc.update("""
                insert into business.contract (
                    id, tenant_id, contract_no, title, contract_type, counterparty_name,
                    amount, currency, status, created_by, updated_by)
                values (?, ?, 'CT-VER', '版本约束', '采购', '北方公司', 1.00, 'CNY', 'draft', ?, ?)
                """, contractId, owner.tenant().id(), owner.user().id(), owner.user().id());
        UUID fileId = insertReadyFile(owner, "owner.pdf");
        insertVersion(owner, contractId, fileId, 1, true);

        assertViolation("contract_version_no_unique", () ->
                insertVersion(owner, contractId, fileId, 1, false));
        assertViolation("contract_version_current_unique", () ->
                insertVersion(owner, contractId, fileId, 2, true));

        UUID foreignFile = insertReadyFile(other, "foreign.pdf");
        assertViolation("contract_version_file_fk", () -> {
            jdbc.queryForObject("select set_config('app.tenant_id', ?, true)", String.class, owner.tenant().id().toString());
            jdbc.update("""
                    insert into business.contract_version (
                        id, tenant_id, contract_id, version_no, version_label, file_id,
                        change_summary, is_current, created_by)
                    values (?, ?, ?, 3, 'V3', ?, '越租户', false, ?)
                    """, UUID.randomUUID(), owner.tenant().id(), contractId, foreignFile, owner.user().id());
        });
    }

    @Test
    void versionsStayImmutableAndFollowContractStatus() throws Exception {
        Account owner = account("version-flow", "ada");
        Account other = account("version-flow-b", "ada");
        String token = login(owner);
        String otherToken = login(other);
        String contractId = createContract(token, "CT-V");
        String firstFile = upload(token, "v1.pdf");
        String secondFile = upload(token, "v2.pdf");
        String removedFile = upload(token, "gone.pdf");
        remember(owner.tenant().id(), UUID.fromString(firstFile));
        remember(owner.tenant().id(), UUID.fromString(secondFile));
        remember(owner.tenant().id(), UUID.fromString(removedFile));
        mockMvc.perform(org.springframework.test.web.servlet.request.MockMvcRequestBuilders
                        .delete("/api/v1/files/" + removedFile)
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk());

        mockMvc.perform(get("/api/v1/contracts/" + contractId + "/versions/current")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").value("当前版本不存在"));

        String created = mockMvc.perform(post("/api/v1/contracts/" + contractId + "/versions")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(versionJson(firstFile, "初始版本", other.user().id())))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.versionNo").value(1))
                .andExpect(jsonPath("$.data.versionLabel").value("V1"))
                .andExpect(jsonPath("$.data.current").value(true))
                .andExpect(jsonPath("$.data.createdBy").value(owner.user().id().toString()))
                .andExpect(jsonPath("$.data.fileId").value(firstFile))
                .andExpect(jsonPath("$.data.filename").value("v1.pdf"))
                .andReturn().getResponse().getContentAsString();
        assertFalse(created.contains("objectKey"));
        String versionId = com.jayway.jsonpath.JsonPath.read(created, "$.data.versionId");

        mockMvc.perform(post("/api/v1/contracts/" + contractId + "/versions")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"fileId\":\"" + removedFile + "\",\"changeSummary\":\"已删除文件\"}"))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").value("文件不存在"));
        mockMvc.perform(get("/api/v1/contracts/" + contractId + "/versions/current")
                        .header("Authorization", "Bearer " + token))
                .andExpect(jsonPath("$.data.versionNo").value(1));

        String foreignFile = upload(otherToken, "foreign.pdf");
        remember(other.tenant().id(), UUID.fromString(foreignFile));
        mockMvc.perform(post("/api/v1/contracts/" + contractId + "/versions")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"fileId\":\"" + foreignFile + "\",\"changeSummary\":\"跨租户文件\"}"))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").value("文件不存在"));
        mockMvc.perform(post("/api/v1/contracts/" + contractId + "/versions")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"fileId\":\"" + UUID.randomUUID() + "\",\"changeSummary\":\"不存在\"}"))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").value("文件不存在"));

        mockMvc.perform(get("/api/v1/contracts/" + contractId + "/versions/" + versionId)
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.changeSummary").value("初始版本"))
                .andExpect(jsonPath("$.data.checksumSha256").exists());
        assertArrayEquals(PDF, download(token, firstFile));
        mockMvc.perform(get("/api/v1/contracts/" + contractId + "/files")
                        .header("Authorization", "Bearer " + token))
                .andExpect(jsonPath("$.data.length()").value(0));

        change(token, contractId, "pending_review");
        rejectVersion(token, contractId, secondFile);
        change(token, contractId, "rejected");
        mockMvc.perform(post("/api/v1/contracts/" + contractId + "/versions")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"fileId\":\"" + secondFile + "\",\"changeSummary\":\"驳回后修订\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.versionNo").value(2))
                .andExpect(jsonPath("$.data.versionLabel").value("V2"))
                .andExpect(jsonPath("$.data.current").value(true));
        mockMvc.perform(get("/api/v1/contracts/" + contractId + "/versions")
                        .header("Authorization", "Bearer " + token))
                .andExpect(jsonPath("$.data.length()").value(2))
                .andExpect(jsonPath("$.data[0].versionNo").value(2))
                .andExpect(jsonPath("$.data[0].current").value(true))
                .andExpect(jsonPath("$.data[1].versionNo").value(1))
                .andExpect(jsonPath("$.data[1].current").value(false))
                .andExpect(jsonPath("$.data[1].fileId").value(firstFile));

        change(token, contractId, "draft");
        change(token, contractId, "pending_review");
        change(token, contractId, "approved");
        rejectVersion(token, contractId, secondFile);
        change(token, contractId, "effective");
        rejectVersion(token, contractId, secondFile);
        change(token, contractId, "expired");
        rejectVersion(token, contractId, secondFile);
        change(token, contractId, "terminated");
        rejectVersion(token, contractId, secondFile);

        mockMvc.perform(get("/api/v1/contracts/" + contractId + "/versions/current")
                        .header("Authorization", "Bearer " + otherToken))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").value("合同不存在"));
        mockMvc.perform(get("/api/v1/contracts/" + contractId + "/versions")
                        .header("Authorization", "Bearer " + otherToken))
                .andExpect(status().isNotFound());
        mockMvc.perform(get("/api/v1/contracts/" + contractId + "/versions/" + versionId)
                        .header("Authorization", "Bearer " + otherToken))
                .andExpect(status().isNotFound());
        mockMvc.perform(post("/api/v1/contracts/" + contractId + "/versions")
                        .header("Authorization", "Bearer " + otherToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"fileId\":\"" + foreignFile + "\",\"changeSummary\":\"越权\"}"))
                .andExpect(status().isNotFound());
        mockMvc.perform(post("/api/v1/contracts/" + contractId + "/versions")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"fileId\":\"" + firstFile + "\",\"changeSummary\":\"匿名\"}"))
                .andExpect(status().isUnauthorized());

        jdbc.queryForObject("select set_config('app.tenant_id', ?, true)", String.class, other.tenant().id().toString());
        Integer hidden = jdbc.queryForObject(
                "select count(*) from business.contract_version where contract_id = ?",
                Integer.class, UUID.fromString(contractId));
        jdbc.queryForObject("select set_config('app.tenant_id', ?, true)", String.class, owner.tenant().id().toString());
        Integer visible = jdbc.queryForObject(
                "select count(*) from business.contract_version where contract_id = ?",
                Integer.class, UUID.fromString(contractId));
        assertEquals(0, hidden);
        assertEquals(2, visible);
    }

    private void rejectVersion(String token, String contractId, String fileId) throws Exception {
        mockMvc.perform(post("/api/v1/contracts/" + contractId + "/versions")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"fileId\":\"" + fileId + "\",\"changeSummary\":\"不允许\"}"))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.message").value("当前状态不允许创建版本"));
    }

    private void assertViolation(String constraint, Runnable action) {
        TransactionTemplate nested = new TransactionTemplate(transactionManager);
        nested.setPropagationBehavior(TransactionDefinition.PROPAGATION_NESTED);
        DataIntegrityViolationException exception = org.junit.jupiter.api.Assertions.assertThrows(
                DataIntegrityViolationException.class, () -> nested.executeWithoutResult(status -> action.run()));
        assertTrue(exception.getMostSpecificCause().getMessage().contains(constraint));
    }

    private UUID insertReadyFile(Account account, String filename) {
        jdbc.queryForObject("select set_config('app.tenant_id', ?, true)", String.class, account.tenant().id().toString());
        UUID fileId = UUID.randomUUID();
        jdbc.update("""
                insert into business.stored_file (
                    id, tenant_id, filename, object_key, bucket_name, media_type,
                    byte_size, extension, checksum_sha256, uploaded_by, status)
                values (?, ?, ?, ?, 'enterprise-agent', 'application/pdf', 4, 'pdf',
                        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', ?, 'ready')
                """, fileId, account.tenant().id(), filename, account.tenant().id() + "/files/" + fileId + ".pdf",
                account.user().id());
        return fileId;
    }

    private void insertVersion(Account account, UUID contractId, UUID fileId, int versionNo, boolean current) {
        jdbc.queryForObject("select set_config('app.tenant_id', ?, true)", String.class, account.tenant().id().toString());
        jdbc.update("""
                insert into business.contract_version (
                    id, tenant_id, contract_id, version_no, version_label, file_id,
                    change_summary, is_current, created_by)
                values (?, ?, ?, ?, ?, ?, '说明', ?, ?)
                """, UUID.randomUUID(), account.tenant().id(), contractId, versionNo, "V" + versionNo,
                fileId, current, account.user().id());
    }

    private Account account(String tenantCode, String username) {
        TenantView tenant = identity.createTenant(tenantCode, tenantCode);
        TenantContext.set(tenant.id());
        UserView user = identity.createUser(username, PASSWORD, username);
        TenantContext.clear();
        return new Account(tenant, user);
    }

    private String login(Account account) throws Exception {
        String body = mockMvc.perform(post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"tenantCode\":\"" + account.tenant().code()
                                + "\",\"username\":\"" + account.user().loginName()
                                + "\",\"password\":\"" + PASSWORD + "\"}"))
                .andExpect(status().isOk())
                .andReturn().getResponse().getContentAsString();
        return com.jayway.jsonpath.JsonPath.read(body, "$.data.token");
    }

    private String createContract(String token, String number) throws Exception {
        String body = mockMvc.perform(post("/api/v1/contracts")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"contractNo\":\"" + number + "\",\"title\":\"版本合同\",\"contractType\":\"采购\","
                                + "\"counterpartyName\":\"北方公司\",\"amount\":1.00,\"currency\":\"CNY\"}"))
                .andExpect(status().isOk())
                .andReturn().getResponse().getContentAsString();
        return com.jayway.jsonpath.JsonPath.read(body, "$.data.id");
    }

    private void change(String token, String contractId, String status) throws Exception {
        mockMvc.perform(post("/api/v1/contracts/" + contractId + "/status")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"status\":\"" + status + "\"}"))
                .andExpect(status().isOk());
    }

    private String upload(String token, String filename) throws Exception {
        String body = mockMvc.perform(multipart("/api/v1/files/upload")
                        .file(new MockMultipartFile("file", filename, "application/pdf", PDF))
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andReturn().getResponse().getContentAsString();
        return com.jayway.jsonpath.JsonPath.read(body, "$.data.id");
    }

    private byte[] download(String token, String fileId) throws Exception {
        MvcResult started = mockMvc.perform(get("/api/v1/files/" + fileId + "/download")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(request().asyncStarted())
                .andReturn();
        started.getAsyncResult();
        return started.getResponse().getContentAsByteArray();
    }

    private void remember(UUID tenantId, UUID fileId) {
        jdbc.queryForObject("select set_config('app.tenant_id', ?, true)", String.class, tenantId.toString());
        objects.add(jdbc.queryForObject(
                "select object_key from business.stored_file where id = ?", String.class, fileId));
    }

    private static String versionJson(String fileId, String summary, UUID otherUser) {
        return "{\"fileId\":\"" + fileId + "\",\"changeSummary\":\"" + summary
                + "\",\"tenantId\":\"" + UUID.randomUUID()
                + "\",\"createdBy\":\"" + otherUser
                + "\",\"versionNo\":9,\"isCurrent\":false}";
    }

    private record Account(TenantView tenant, UserView user) {
    }
}
