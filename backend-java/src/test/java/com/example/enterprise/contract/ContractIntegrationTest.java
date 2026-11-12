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
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.multipart;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.request;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest
@ActiveProfiles("dev")
@AutoConfigureMockMvc
@Transactional
@EnabledIfEnvironmentVariable(named = "MINIO_ROOT_PASSWORD", matches = ".+")
class ContractIntegrationTest {

    private static final String PASSWORD = "plain-secret-day13";
    private static final byte[] PDF = "%PDF-1.4\ncontract\n%%EOF\n".getBytes(StandardCharsets.US_ASCII);

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
    void migrationCreatesContractTablesAndConstraints() {
        Boolean migrated = jdbc.queryForObject("""
                select success from business.flyway_schema_history where version = '3'
                """, Boolean.class);
        Integer constraints = jdbc.queryForObject("""
                select count(*) from pg_constraint
                where conname in (
                    'contract_no_unique', 'contract_id_tenant_unique',
                    'contract_creator_fk', 'contract_updater_fk',
                    'contract_file_link_unique', 'contract_file_contract_fk',
                    'contract_file_file_fk', 'contract_file_creator_fk',
                    'stored_file_id_tenant_unique')
                """, Integer.class);
        Integer indexes = jdbc.queryForObject("""
                select count(*) from pg_indexes
                where schemaname = 'business'
                  and indexname in ('contract_tenant_idx', 'contract_tenant_status_idx', 'contract_file_tenant_idx')
                """, Integer.class);
        Integer forced = jdbc.queryForObject("""
                select count(*)
                from pg_class c
                join pg_namespace n on n.oid = c.relnamespace
                where n.nspname = 'business'
                  and c.relname in ('contract', 'contract_file')
                  and c.relrowsecurity and c.relforcerowsecurity
                """, Integer.class);
        Boolean agentDenied = jdbc.queryForObject(
                "select not has_schema_privilege(current_user, 'agent', 'USAGE')", Boolean.class);
        assertEquals(Boolean.TRUE, migrated);
        assertEquals(9, constraints);
        assertEquals(3, indexes);
        assertEquals(2, forced);
        assertEquals(Boolean.TRUE, agentDenied);
    }

    @Test
    void createGetUpdateAndFilterStayInsideTheTenant() throws Exception {
        Account account = account("contract-acme", "ada");
        String token = login(account);
        String otherUser = UUID.randomUUID().toString();

        String created = mockMvc.perform(post("/api/v1/contracts")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(withExtra(contractJson("CT-001", "采购合同", "采购", "北方公司", "1000.50",
                                "2026-01-15", "2026-02-01", "2026-12-31"),
                                "\"tenantId\":\"" + UUID.randomUUID()
                                        + "\",\"createdBy\":\"" + otherUser
                                        + "\",\"status\":\"effective\"")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.contractNo").value("CT-001"))
                .andExpect(jsonPath("$.data.title").value("采购合同"))
                .andExpect(jsonPath("$.data.status").value("draft"))
                .andExpect(jsonPath("$.data.currency").value("CNY"))
                .andExpect(jsonPath("$.data.createdBy").value(account.user().id().toString()))
                .andExpect(jsonPath("$.data.amount").value(1000.50))
                .andReturn().getResponse().getContentAsString();
        String contractId = com.jayway.jsonpath.JsonPath.read(created, "$.data.id");

        mockMvc.perform(get("/api/v1/contracts/" + contractId)
                        .header("Authorization", "Bearer " + token)
                        .param("tenantId", UUID.randomUUID().toString()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.files.length()").value(0))
                .andExpect(jsonPath("$.data.createdBy").value(account.user().id().toString()));

        mockMvc.perform(post("/api/v1/contracts")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(contractJson("CT-002", "销售合同", "销售", "南方公司", "20.00",
                                "2026-06-01", null, null)))
                .andExpect(status().isOk());

        mockMvc.perform(get("/api/v1/contracts")
                        .header("Authorization", "Bearer " + token)
                        .param("contractName", "采购")
                        .param("counterpartyName", "北方")
                        .param("contractType", "采购")
                        .param("status", "draft")
                        .param("contractNo", "CT-001")
                        .param("signDateFrom", "2026-01-01")
                        .param("signDateTo", "2026-03-01")
                        .param("tenantId", UUID.randomUUID().toString()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.total").value(1))
                .andExpect(jsonPath("$.data.items[0].contractNo").value("CT-001"));

        mockMvc.perform(get("/api/v1/contracts")
                        .header("Authorization", "Bearer " + token)
                        .param("contractName", "%"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.total").value(0));

        mockMvc.perform(put("/api/v1/contracts/" + contractId)
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(withExtra(contractJson("CT-001", "采购合同修订", "采购", "北方公司", "1100.00",
                                "2026-01-15", "2026-02-01", "2026-12-31"),
                                "\"tenantId\":\"" + UUID.randomUUID()
                                        + "\",\"createdBy\":\"" + otherUser
                                        + "\",\"updatedBy\":\"" + otherUser
                                        + "\",\"status\":\"terminated\"")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.title").value("采购合同修订"))
                .andExpect(jsonPath("$.data.status").value("draft"))
                .andExpect(jsonPath("$.data.createdBy").value(account.user().id().toString()))
                .andExpect(jsonPath("$.data.updatedBy").value(account.user().id().toString()));

        mockMvc.perform(post("/api/v1/contracts")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(contractJson("CT-001", "重复", "采购", "北方公司", "1.00", null, null, null)))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.message").value("合同编号已存在"));
    }

    @Test
    void statusChangesFollowTheServerRules() throws Exception {
        Account account = account("contract-status", "ada");
        String token = login(account);
        String contractId = create(token, "CT-S", "状态合同", "采购", "北方公司");

        mockMvc.perform(post("/api/v1/contracts/" + contractId + "/status")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"status\":\"effective\"}"))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.message").value("不允许从草稿变更为已生效"));
        mockMvc.perform(get("/api/v1/contracts/" + contractId).header("Authorization", "Bearer " + token))
                .andExpect(jsonPath("$.data.status").value("draft"));

        change(token, contractId, "pending_review");
        mockMvc.perform(put("/api/v1/contracts/" + contractId)
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(contractJson("CT-S", "不能改", "采购", "北方公司", "1.00", null, null, null)))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.message").value("当前状态不允许修改"));

        change(token, contractId, "rejected");
        mockMvc.perform(put("/api/v1/contracts/" + contractId)
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(contractJson("CT-S", "驳回后可改", "采购", "北方公司", "2.00", null, null, null)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.title").value("驳回后可改"))
                .andExpect(jsonPath("$.data.status").value("rejected"));

        change(token, contractId, "draft");
        change(token, contractId, "pending_review");
        change(token, contractId, "approved");
        change(token, contractId, "effective");
        mockMvc.perform(put("/api/v1/contracts/" + contractId)
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(contractJson("CT-S", "生效后不能改", "采购", "北方公司", "2.00", null, null, null)))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.message").value("当前状态不允许修改"));
    }

    @Test
    void fileLinkUsesStoredFileAndUnlinkKeepsTheObject() throws Exception {
        Account owner = account("contract-files", "ada");
        Account other = account("contract-files-b", "ada");
        String ownerToken = login(owner);
        String otherToken = login(other);
        String contractId = create(ownerToken, "CT-F", "文件合同", "采购", "北方公司");
        String ownFile = upload(ownerToken, "own.pdf");
        String foreignFile = upload(otherToken, "foreign.pdf");
        remember(owner.tenant().id(), UUID.fromString(ownFile));
        remember(other.tenant().id(), UUID.fromString(foreignFile));

        mockMvc.perform(post("/api/v1/contracts/" + contractId + "/files")
                        .header("Authorization", "Bearer " + ownerToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"fileId\":\"" + foreignFile + "\",\"fileRole\":\"original\"}"))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").value("文件不存在"));

        mockMvc.perform(post("/api/v1/contracts/" + contractId + "/files")
                        .header("Authorization", "Bearer " + ownerToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"fileId\":\"" + ownFile + "\",\"fileRole\":\"original\",\"tenantId\":\""
                                + other.tenant().id() + "\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.filename").value("own.pdf"))
                .andExpect(jsonPath("$.data.fileRole").value("original"));
        assertFalse(mockMvc.perform(get("/api/v1/contracts/" + contractId + "/files")
                        .header("Authorization", "Bearer " + ownerToken))
                .andReturn().getResponse().getContentAsString().contains("objectKey"));

        mockMvc.perform(post("/api/v1/contracts/" + contractId + "/files")
                        .header("Authorization", "Bearer " + ownerToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"fileId\":\"" + ownFile + "\",\"fileRole\":\"attachment\"}"))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.message").value("文件已关联"));

        mockMvc.perform(get("/api/v1/contracts/" + contractId).header("Authorization", "Bearer " + ownerToken))
                .andExpect(jsonPath("$.data.files[0].fileId").value(ownFile));
        assertArrayEquals(PDF, download(ownerToken, ownFile));

        mockMvc.perform(delete("/api/v1/contracts/" + contractId + "/files/" + ownFile)
                        .header("Authorization", "Bearer " + ownerToken))
                .andExpect(status().isOk());
        mockMvc.perform(get("/api/v1/contracts/" + contractId + "/files").header("Authorization", "Bearer " + ownerToken))
                .andExpect(jsonPath("$.data.length()").value(0));
        mockMvc.perform(get("/api/v1/files/" + ownFile).header("Authorization", "Bearer " + ownerToken))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.status").value("ready"));
        String objectKey = objectKey(owner.tenant().id(), UUID.fromString(ownFile));
        assertTrue(storage.exists(properties.getBucket(), objectKey));

        mockMvc.perform(get("/api/v1/contracts/" + contractId).header("Authorization", "Bearer " + otherToken))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").value("合同不存在"));
        mockMvc.perform(put("/api/v1/contracts/" + contractId)
                        .header("Authorization", "Bearer " + otherToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(contractJson("CT-F", "越权", "采购", "北方公司", "1.00", null, null, null)))
                .andExpect(status().isNotFound());
        mockMvc.perform(post("/api/v1/contracts/" + contractId + "/status")
                        .header("Authorization", "Bearer " + otherToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"status\":\"terminated\"}"))
                .andExpect(status().isNotFound());
        mockMvc.perform(post("/api/v1/contracts/" + contractId + "/files")
                        .header("Authorization", "Bearer " + otherToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"fileId\":\"" + foreignFile + "\",\"fileRole\":\"original\"}"))
                .andExpect(status().isNotFound());
        mockMvc.perform(get("/api/v1/contracts").header("Authorization", "Bearer " + otherToken))
                .andExpect(jsonPath("$.data.total").value(0));
        mockMvc.perform(post("/api/v1/contracts")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(contractJson("CT-X", "匿名", "采购", "北方公司", "1.00", null, null, null)))
                .andExpect(status().isUnauthorized());
    }

    @Test
    void crossTenantFileForeignKeyIsRejected() {
        Account owner = account("contract-fk", "ada");
        Account other = account("contract-fk-b", "ada");
        UserView ownerUser = owner.user();
        UUID contractId = UUID.randomUUID();
        jdbc.queryForObject("select set_config('app.tenant_id', ?, true)", String.class, owner.tenant().id().toString());
        jdbc.update("""
                insert into business.contract (
                    id, tenant_id, contract_no, title, contract_type, counterparty_name,
                    amount, currency, status, created_by, updated_by)
                values (?, ?, 'CT-FK', '外键', '采购', '北方公司', 1.00, 'CNY', 'draft', ?, ?)
                """, contractId, owner.tenant().id(), ownerUser.id(), ownerUser.id());
        UUID foreignFile = UUID.randomUUID();
        jdbc.queryForObject("select set_config('app.tenant_id', ?, true)", String.class, other.tenant().id().toString());
        jdbc.update("""
                insert into business.stored_file (
                    id, tenant_id, filename, object_key, bucket_name, media_type,
                    byte_size, extension, checksum_sha256, uploaded_by, status)
                values (?, ?, 'x.pdf', ?, 'enterprise-agent', 'application/pdf', 4, 'pdf',
                        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', ?, 'ready')
                """, foreignFile, other.tenant().id(), other.tenant().id() + "/files/fk.pdf", other.user().id());
        jdbc.queryForObject("select set_config('app.tenant_id', ?, true)", String.class, owner.tenant().id().toString());
        TransactionTemplate nested = new TransactionTemplate(transactionManager);
        nested.setPropagationBehavior(TransactionDefinition.PROPAGATION_NESTED);
        org.springframework.dao.DataIntegrityViolationException exception = org.junit.jupiter.api.Assertions.assertThrows(
                org.springframework.dao.DataIntegrityViolationException.class,
                () -> nested.executeWithoutResult(status -> jdbc.update("""
                        insert into business.contract_file (id, tenant_id, contract_id, file_id, file_role, created_by)
                        values (?, ?, ?, ?, 'original', ?)
                        """, UUID.randomUUID(), owner.tenant().id(), contractId, foreignFile, ownerUser.id())));
        assertTrue(exception.getMostSpecificCause().getMessage().contains("contract_file_file_fk"));
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

    private String create(String token, String number, String title, String type, String counterparty) throws Exception {
        MvcResult result = mockMvc.perform(post("/api/v1/contracts")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(contractJson(number, title, type, counterparty, "1.00", null, null, null)))
                .andExpect(status().isOk())
                .andReturn();
        return com.jayway.jsonpath.JsonPath.read(result.getResponse().getContentAsString(), "$.data.id");
    }

    private void change(String token, String contractId, String status) throws Exception {
        mockMvc.perform(post("/api/v1/contracts/" + contractId + "/status")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"status\":\"" + status + "\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.status").value(status));
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

    private String upload(String token, String filename) throws Exception {
        String body = mockMvc.perform(multipart("/api/v1/files/upload")
                        .file(new MockMultipartFile("file", filename, "application/pdf", PDF))
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andReturn().getResponse().getContentAsString();
        return com.jayway.jsonpath.JsonPath.read(body, "$.data.id");
    }

    private void remember(UUID tenantId, UUID fileId) {
        objects.add(objectKey(tenantId, fileId));
    }

    private String objectKey(UUID tenantId, UUID fileId) {
        jdbc.queryForObject("select set_config('app.tenant_id', ?, true)", String.class, tenantId.toString());
        return jdbc.queryForObject("select object_key from business.stored_file where id = ?", String.class, fileId);
    }

    private static String contractJson(
            String number, String title, String type, String counterparty, String amount,
            String signDate, String effectiveDate, String expiryDate) {
        return "{\"contractNo\":\"" + number
                + "\",\"title\":\"" + title
                + "\",\"contractType\":\"" + type
                + "\",\"counterpartyName\":\"" + counterparty
                + "\",\"amount\":" + amount
                + ",\"currency\":\"cny\""
                + dateField("signDate", signDate)
                + dateField("effectiveDate", effectiveDate)
                + dateField("expiryDate", expiryDate)
                + "}";
    }

    private static String dateField(String name, String value) {
        return value == null ? "" : ",\"" + name + "\":\"" + value + "\"";
    }

    private static String withExtra(String json, String extra) {
        return json.substring(0, json.length() - 1) + "," + extra + "}";
    }

    private record Account(TenantView tenant, UserView user) {
    }
}
