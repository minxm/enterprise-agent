package com.example.enterprise.internal;

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
import org.springframework.data.redis.core.Cursor;
import org.springframework.data.redis.core.ScanOptions;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.transaction.annotation.Transactional;

import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * Real PostgreSQL. The agent token and ticket are test values. MinIO is not used;
 * the version file row is inserted as metadata only.
 */
@SpringBootTest(properties = {
        "agent.service.internal-token=test-internal-token",
        "enterprise.actor-ticket.prefix=actor-ticket:contract-test:"
})
@ActiveProfiles("dev")
@AutoConfigureMockMvc
@Transactional
@EnabledIfEnvironmentVariable(named = "BUSINESS_APP_PASSWORD", matches = ".+")
class InternalContractQueryTest {

    private static final String TOKEN = "test-internal-token";
    private static final String TICKET_PREFIX = "actor-ticket:contract-test:";
    private static final String PASSWORD = "plain-secret-day33";
    private static final String OBJECT_KEY = "secret-object-key-should-not-leak";
    private static final String CHECKSUM = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

    @Autowired
    private IdentityService identity;

    @Autowired
    private ActorTicketStore tickets;

    @Autowired
    private StringRedisTemplate redis;

    @Autowired
    private JdbcTemplate jdbc;

    @Autowired
    private MockMvc mockMvc;

    @AfterEach
    void clear() {
        TenantContext.clear();
        SecurityContextHolder.clearContext();
        deleteTicketKeys();
    }

    @Test
    void returnsBasicInfoCurrentVersionAndApprovalStatusForTheTicketTenant() throws Exception {
        String suffix = UUID.randomUUID().toString().substring(0, 8);
        Account reader = account("cq-" + suffix, "reader-" + suffix);
        grant(reader, "contract_manager");
        String access = login(reader);
        String contractId = createContract(access, "HT-2026-001", "年度采购合同");
        String fileId = insertFile(reader, "采购合同.pdf");
        createVersion(access, contractId, fileId);
        changeStatus(access, contractId, "pending_review");
        int before = contractCount(reader.tenant().id());

        String body = mockMvc.perform(get("/internal/v1/contracts")
                        .param("contractNo", "HT-2026-001")
                        .header("X-Agent-Token", TOKEN)
                        .header("X-Actor-Ticket", tickets.issue(reader.tenant().id(), reader.user().id()))
                        .header("X-Tenant-Id", UUID.randomUUID().toString())
                        .header("X-User-Id", UUID.randomUUID().toString()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.contractNo").value("HT-2026-001"))
                .andExpect(jsonPath("$.data.title").value("年度采购合同"))
                .andExpect(jsonPath("$.data.contractType").value("采购"))
                .andExpect(jsonPath("$.data.counterpartyName").value("南方公司"))
                .andExpect(jsonPath("$.data.amount").value("1.00"))
                .andExpect(jsonPath("$.data.currency").value("CNY"))
                .andExpect(jsonPath("$.data.status").value("pending_review"))
                .andExpect(jsonPath("$.data.statusLabel").value("待审核"))
                .andExpect(jsonPath("$.data.currentVersion.versionLabel").value("V1"))
                .andExpect(jsonPath("$.data.currentVersion.versionNo").value(1))
                .andExpect(jsonPath("$.data.currentVersion.changeSummary").value("首版"))
                .andExpect(jsonPath("$.data.currentVersion.filename").value("采购合同.pdf"))
                .andReturn().getResponse().getContentAsString();

        assertFalse(body.contains(OBJECT_KEY));
        assertFalse(body.contains(CHECKSUM));
        assertFalse(body.contains("password"));
        assertFalse(body.contains("object_key"));
        assertFalse(body.contains("objectKey"));
        assertEquals(before, contractCount(reader.tenant().id()));
    }

    @Test
    void missingContractIsNotFoundInsideTheSameTenant() throws Exception {
        String suffix = UUID.randomUUID().toString().substring(0, 8);
        Account reader = account("cq-miss-" + suffix, "reader-" + suffix);
        grant(reader, "contract_manager");

        mockMvc.perform(get("/internal/v1/contracts")
                        .param("contractNo", "HT-2026-404")
                        .header("X-Agent-Token", TOKEN)
                        .header("X-Actor-Ticket", tickets.issue(reader.tenant().id(), reader.user().id())))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").value("合同不存在"));
    }

    @Test
    void anotherTenantCannotSeeTheContract() throws Exception {
        String suffix = UUID.randomUUID().toString().substring(0, 8);
        Account owner = account("cq-own-" + suffix, "owner-" + suffix);
        grant(owner, "contract_manager");
        createContract(login(owner), "HT-2026-001", "甲方合同");
        Account outsider = account("cq-out-" + suffix, "outsider-" + suffix);
        grant(outsider, "approver");

        mockMvc.perform(get("/internal/v1/contracts")
                        .param("contractNo", "HT-2026-001")
                        .header("X-Agent-Token", TOKEN)
                        .header("X-Actor-Ticket", tickets.issue(outsider.tenant().id(), outsider.user().id())))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").value("合同不存在"));
    }

    @Test
    void userWithoutContractReadIsForbiddenAndLearnsNothing() throws Exception {
        String suffix = UUID.randomUUID().toString().substring(0, 8);
        Account owner = account("cq-perm-" + suffix, "owner-" + suffix);
        grant(owner, "contract_manager");
        createContract(login(owner), "HT-2026-001", "机密标题不应出现");
        TenantContext.set(owner.tenant().id());
        UserView stranger = identity.createUser("stranger-" + suffix, PASSWORD, "stranger");
        TenantContext.clear();

        String ticket = tickets.issue(owner.tenant().id(), stranger.id());
        String body = mockMvc.perform(get("/internal/v1/contracts")
                        .param("contractNo", "HT-2026-001")
                        .header("X-Agent-Token", TOKEN)
                        .header("X-Actor-Ticket", ticket))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.message").value("无权查询合同"))
                .andReturn().getResponse().getContentAsString();
        assertFalse(body.contains("机密标题不应出现"));
        assertTrue(tickets.consume(ticket).isEmpty());
    }

    @Test
    void missingOrWrongTokenIsRejected() throws Exception {
        String suffix = UUID.randomUUID().toString().substring(0, 8);
        Account reader = account("cq-auth-" + suffix, "reader-" + suffix);
        grant(reader, "contract_manager");
        String ticket = tickets.issue(reader.tenant().id(), reader.user().id());

        mockMvc.perform(get("/internal/v1/contracts")
                        .param("contractNo", "HT-2026-001")
                        .header("X-Actor-Ticket", ticket))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.message").value("未认证"));
        mockMvc.perform(get("/internal/v1/contracts")
                        .param("contractNo", "HT-2026-001")
                        .header("X-Agent-Token", "wrong-token")
                        .header("X-Actor-Ticket", ticket))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.message").value("未认证"));
        mockMvc.perform(get("/internal/v1/contracts")
                        .param("contractNo", "HT-2026-001")
                        .header("X-Agent-Token", TOKEN))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.message").value("未认证"));
        assertEquals(reader.user().id(), tickets.consume(ticket).orElseThrow().actorId());
    }

    @Test
    void aSuccessfulQueryConsumesTheTicket() throws Exception {
        String suffix = UUID.randomUUID().toString().substring(0, 8);
        Account reader = account("once-" + suffix, "reader-" + suffix);
        grant(reader, "contract_manager");
        createContract(login(reader), "HT-2026-001", "只能查询一次");
        String ticket = tickets.issue(reader.tenant().id(), reader.user().id());

        mockMvc.perform(get("/internal/v1/contracts")
                        .param("contractNo", "HT-2026-001")
                        .header("X-Agent-Token", TOKEN)
                        .header("X-Actor-Ticket", ticket))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.title").value("只能查询一次"));
        String again = mockMvc.perform(get("/internal/v1/contracts")
                        .param("contractNo", "HT-2026-001")
                        .header("X-Agent-Token", TOKEN)
                        .header("X-Actor-Ticket", ticket))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.message").value("未认证"))
                .andReturn().getResponse().getContentAsString();
        assertFalse(again.contains("只能查询一次"));
    }

    @Test
    void identityParametersAndWritesAreRejected() throws Exception {
        mockMvc.perform(get("/internal/v1/contracts")
                        .param("contractNo", "HT-2026-001")
                        .param("tenantId", UUID.randomUUID().toString())
                        .header("X-Agent-Token", TOKEN)
                        .header("X-Actor-Ticket", "unused"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.message").value("请求参数不合法"));
        mockMvc.perform(post("/internal/v1/contracts")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"contractNo\":\"HT-2026-001\"}")
                        .header("X-Agent-Token", TOKEN))
                .andExpect(status().isInternalServerError())
                .andExpect(jsonPath("$.message").value("服务内部错误"));
    }

    private Account account(String tenantCode, String username) {
        TenantView tenant = identity.createTenant(tenantCode, tenantCode);
        TenantContext.set(tenant.id());
        UserView user = identity.createUser(username, PASSWORD, username);
        TenantContext.clear();
        return new Account(tenant, user);
    }

    private void grant(Account account, String roleCode) {
        TenantContext.set(account.tenant().id());
        jdbc.queryForObject("select set_config('app.tenant_id', ?, true)", String.class, account.tenant().id().toString());
        UUID roleId = jdbc.queryForObject(
                "select id from business.role where tenant_id = ? and code = ?",
                UUID.class, account.tenant().id(), roleCode);
        identity.assignRole(account.user().id(), roleId);
        TenantContext.clear();
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

    private String createContract(String token, String number, String title) throws Exception {
        MvcResult result = mockMvc.perform(post("/api/v1/contracts")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"contractNo\":\"" + number + "\",\"title\":\"" + title
                                + "\",\"contractType\":\"采购\",\"counterpartyName\":\"南方公司\","
                                + "\"amount\":1.00,\"currency\":\"cny\"}"))
                .andExpect(status().isOk())
                .andReturn();
        return com.jayway.jsonpath.JsonPath.read(result.getResponse().getContentAsString(), "$.data.id");
    }

    private String insertFile(Account account, String filename) {
        UUID fileId = UUID.randomUUID();
        jdbc.queryForObject("select set_config('app.tenant_id', ?, true)", String.class, account.tenant().id().toString());
        jdbc.update("""
                insert into business.stored_file (
                    id, tenant_id, filename, object_key, bucket_name, media_type,
                    byte_size, extension, checksum_sha256, uploaded_by, status)
                values (?, ?, ?, ?, 'enterprise-agent', 'application/pdf', 4, 'pdf', ?, ?, 'ready')
                """, fileId, account.tenant().id(), filename, account.tenant().id() + "/" + OBJECT_KEY,
                CHECKSUM, account.user().id());
        return fileId.toString();
    }

    private void createVersion(String token, String contractId, String fileId) throws Exception {
        mockMvc.perform(post("/api/v1/contracts/" + contractId + "/versions")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"fileId\":\"" + fileId + "\",\"changeSummary\":\"首版\"}"))
                .andExpect(status().isOk());
    }

    private void changeStatus(String token, String contractId, String target) throws Exception {
        mockMvc.perform(post("/api/v1/contracts/" + contractId + "/status")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"status\":\"" + target + "\"}"))
                .andExpect(status().isOk());
    }

    private void deleteTicketKeys() {
        List<String> keys = new ArrayList<>();
        ScanOptions options = ScanOptions.scanOptions().match(TICKET_PREFIX + "*").count(100).build();
        try (Cursor<String> cursor = redis.scan(options)) {
            cursor.forEachRemaining(keys::add);
        }
        if (!keys.isEmpty()) {
            redis.delete(keys);
        }
    }

    private int contractCount(UUID tenantId) {
        jdbc.queryForObject("select set_config('app.tenant_id', ?, true)", String.class, tenantId.toString());
        Integer count = jdbc.queryForObject("select count(*) from business.contract", Integer.class);
        return count == null ? 0 : count;
    }

    private record Account(TenantView tenant, UserView user) {
    }
}
