package com.example.enterprise.file;

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
import org.springframework.transaction.annotation.Transactional;

import java.nio.charset.StandardCharsets;
import java.time.LocalDate;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.multipart;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.request;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest
@ActiveProfiles("dev")
@AutoConfigureMockMvc
@Transactional
@EnabledIfEnvironmentVariable(named = "MINIO_ROOT_PASSWORD", matches = ".+")
class FileIntegrationTest {

    private static final String PASSWORD = "plain-secret-day12";
    private static final byte[] PDF = "%PDF-1.4\nfile-body\n%%EOF\n".getBytes(StandardCharsets.US_ASCII);

    @Autowired
    private IdentityService identity;

    @Autowired
    private JdbcTemplate jdbc;

    @Autowired
    private ObjectStorage storage;

    @Autowired
    private StorageProperties properties;

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
    void migrationAndBucketAreReady() {
        Boolean migrated = jdbc.queryForObject("""
                select success from business.flyway_schema_history where version = '2'
                """, Boolean.class);
        Integer forced = jdbc.queryForObject("""
                select count(*)
                from pg_class c
                join pg_namespace n on n.oid = c.relnamespace
                where n.nspname = 'business' and c.relname = 'stored_file' and c.relrowsecurity and c.relforcerowsecurity
                """, Integer.class);
        assertEquals(Boolean.TRUE, migrated);
        assertEquals(1, forced);
        assertEquals("enterprise-agent", properties.getBucket());
        storage.exists(properties.getBucket(), "files/readiness-probe");
    }

    @Test
    void uploadDownloadAndDeleteRoundTripOnMinio() throws Exception {
        Account account = account("files-acme", "ada");
        String token = login(account, PASSWORD);

        MvcResult uploaded = mockMvc.perform(multipart("/api/v1/files/upload")
                        .file(new MockMultipartFile("file", "..\\..\\notes.pdf", "application/octet-stream", PDF))
                        .param("tenantId", UUID.randomUUID().toString())
                        .param("objectKey", "../client-controlled.pdf")
                        .header("Authorization", "Bearer " + token)
                        .header("X-Tenant-Id", UUID.randomUUID().toString()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.filename").value("notes.pdf"))
                .andExpect(jsonPath("$.data.status").value("ready"))
                .andExpect(jsonPath("$.data.extension").value("pdf"))
                .andExpect(jsonPath("$.data.uploadedBy").value(account.user().id().toString()))
                .andExpect(header().exists("X-Request-ID"))
                .andReturn();
        String body = uploaded.getResponse().getContentAsString();
        assertFalse(body.contains("objectKey"));
        assertFalse(body.contains("bucketName"));
        assertFalse(body.contains("secretKey"));
        String fileId = com.jayway.jsonpath.JsonPath.read(body, "$.data.id");
        String checksum = com.jayway.jsonpath.JsonPath.read(body, "$.data.checksumSha256");
        String objectKey = objectKey(account.tenant().id(), UUID.fromString(fileId));
        objects.add(objectKey);
        assertFalse(objectKey.contains("notes"));
        assertFalse(objectKey.contains(".."));
        assertFalse(objectKey.contains("client-controlled"));
        LocalDate today = LocalDate.now(ZoneOffset.UTC);
        assertEquals(account.tenant().id() + "/files/" + today.getYear() + "/"
                + String.format("%02d", today.getMonthValue()) + "/" + fileId + ".pdf", objectKey);

        String metadata = jdbc.queryForObject("""
                select checksum_sha256 || '|' || bucket_name || '|' || byte_size || '|' || status
                from business.stored_file where id = ?
                """, String.class, UUID.fromString(fileId));
        assertEquals(checksum + "|enterprise-agent|" + PDF.length + "|ready", metadata);
        assertEquals(true, storage.exists(properties.getBucket(), objectKey));

        MvcResult started = mockMvc.perform(get("/api/v1/files/" + fileId + "/download")
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(header().string("Content-Disposition", org.hamcrest.Matchers.containsString("notes.pdf")))
                .andExpect(request().asyncStarted())
                .andReturn();
        started.getAsyncResult();
        byte[] downloaded = started.getResponse().getContentAsByteArray();
        assertArrayEquals(PDF, downloaded);

        mockMvc.perform(get("/api/v1/files").header("Authorization", "Bearer " + token).param("tenantId", UUID.randomUUID().toString()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.total").value(1))
                .andExpect(jsonPath("$.data.items[0].id").value(fileId));

        mockMvc.perform(delete("/api/v1/files/" + fileId).header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.status").value("deleted"));
        mockMvc.perform(delete("/api/v1/files/" + fileId).header("Authorization", "Bearer " + token))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.status").value("deleted"));
        assertEquals(false, storage.exists(properties.getBucket(), objectKey));
        mockMvc.perform(get("/api/v1/files/" + fileId).header("Authorization", "Bearer " + token))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").value("文件不存在"));
    }

    @Test
    void rejectsEmptyOversizedAndDisallowedFiles() throws Exception {
        Account account = account("files-invalid", "ada");
        String token = login(account, PASSWORD);

        mockMvc.perform(multipart("/api/v1/files/upload")
                        .file(new MockMultipartFile("file", "empty.pdf", "application/pdf", new byte[0]))
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.message").value("文件不能为空"));

        mockMvc.perform(multipart("/api/v1/files/upload")
                        .file(new MockMultipartFile("file", "virus.exe", "application/pdf", PDF))
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.message").value("不支持的文件类型"));

        byte[] oversized = new byte[(int) properties.getMaxBytes() + 1];
        System.arraycopy(PDF, 0, oversized, 0, PDF.length);
        mockMvc.perform(multipart("/api/v1/files/upload")
                        .file(new MockMultipartFile("file", "big.pdf", "application/pdf", oversized))
                        .header("Authorization", "Bearer " + token))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.message").value("文件大小超出限制"));

        mockMvc.perform(multipart("/api/v1/files/upload")
                        .file(new MockMultipartFile("file", "a.pdf", "application/pdf", PDF)))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.message").value("未认证"));
    }

    @Test
    void otherTenantCannotReadOrDeleteTheFile() throws Exception {
        Account owner = account("files-owner", "ada");
        Account other = account("files-other", "ada");
        String ownerToken = login(owner, PASSWORD);
        String otherToken = login(other, PASSWORD);

        String body = mockMvc.perform(multipart("/api/v1/files/upload")
                        .file(new MockMultipartFile("file", "owned.pdf", "application/pdf", PDF))
                        .header("Authorization", "Bearer " + ownerToken))
                .andExpect(status().isOk())
                .andReturn()
                .getResponse()
                .getContentAsString();
        String fileId = com.jayway.jsonpath.JsonPath.read(body, "$.data.id");
        objects.add(objectKey(owner.tenant().id(), UUID.fromString(fileId)));

        mockMvc.perform(get("/api/v1/files/" + fileId).header("Authorization", "Bearer " + otherToken))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").value("文件不存在"));
        mockMvc.perform(get("/api/v1/files/" + fileId + "/download").header("Authorization", "Bearer " + otherToken))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").value("文件不存在"));
        mockMvc.perform(delete("/api/v1/files/" + fileId).header("Authorization", "Bearer " + otherToken))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").value("文件不存在"));
        mockMvc.perform(get("/api/v1/files").header("Authorization", "Bearer " + otherToken))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.total").value(0));
        mockMvc.perform(get("/api/v1/files/" + fileId).header("Authorization", "Bearer " + ownerToken))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.data.id").value(fileId));
    }

    private Account account(String tenantCode, String username) {
        TenantView tenant = identity.createTenant(tenantCode, tenantCode);
        TenantContext.set(tenant.id());
        UserView user = identity.createUser(username, PASSWORD, username);
        TenantContext.clear();
        return new Account(tenant, user);
    }

    private String login(Account account, String password) throws Exception {
        String body = mockMvc.perform(post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"tenantCode\":\"" + account.tenant().code()
                                + "\",\"username\":\"" + account.user().loginName()
                                + "\",\"password\":\"" + password + "\"}"))
                .andExpect(status().isOk())
                .andReturn()
                .getResponse()
                .getContentAsString();
        return com.jayway.jsonpath.JsonPath.read(body, "$.data.token");
    }

    private String objectKey(UUID tenantId, UUID fileId) {
        jdbc.queryForObject("select set_config('app.tenant_id', ?, true)", String.class, tenantId.toString());
        return jdbc.queryForObject("select object_key from business.stored_file where id = ?", String.class, fileId);
    }

    private record Account(TenantView tenant, UserView user) {
    }
}
