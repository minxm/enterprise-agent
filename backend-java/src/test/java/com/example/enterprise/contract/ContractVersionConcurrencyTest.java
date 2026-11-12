package com.example.enterprise.contract;

import com.example.enterprise.auth.AuthenticatedUser;
import com.example.enterprise.contract.ContractViews.ContractVersionView;
import com.example.enterprise.contract.ContractViews.CreateVersion;
import com.example.enterprise.contract.ContractViews.WriteContract;
import com.example.enterprise.file.FileViews.FileView;
import com.example.enterprise.file.ObjectStorage;
import com.example.enterprise.file.StorageProperties;
import com.example.enterprise.file.StoredFileService;
import com.example.enterprise.identity.IdentityService;
import com.example.enterprise.identity.IdentityViews.TenantView;
import com.example.enterprise.identity.IdentityViews.UserView;
import com.example.enterprise.identity.TenantContext;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.stream.Collectors;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

@SpringBootTest
@ActiveProfiles("dev")
@EnabledIfEnvironmentVariable(named = "MINIO_ROOT_PASSWORD", matches = ".+")
class ContractVersionConcurrencyTest {

    private static final byte[] PDF = "%PDF-1.4\nconcurrent\n%%EOF\n".getBytes(StandardCharsets.US_ASCII);

    @Autowired
    private IdentityService identity;

    @Autowired
    private StoredFileService files;

    @Autowired
    private ContractService contracts;

    @Autowired
    private JdbcTemplate jdbc;

    @Autowired
    private ObjectStorage storage;

    @Autowired
    private StorageProperties properties;

    @Autowired
    private PlatformTransactionManager transactionManager;

    @Test
    void concurrentCreatesKeepOneCurrentVersionAndDistinctNumbers() throws Exception {
        UUID tenantId = null;
        UUID contractId = null;
        List<UUID> fileIds = new ArrayList<>();
        try {
            String code = "vr-" + UUID.randomUUID().toString().substring(0, 8);
            TenantView tenant = identity.createTenant(code, code);
            tenantId = tenant.id();
            TenantContext.set(tenantId);
            UserView user = identity.createUser("ada", "plain-secret-day14", "ada");
            AuthenticatedUser auth = new AuthenticatedUser(user.id(), tenantId, user.loginName());
            FileView first = files.upload(auth, "a.pdf", PDF);
            FileView second = files.upload(auth, "b.pdf", PDF);
            fileIds.add(first.id());
            fileIds.add(second.id());
            UUID createdId = contracts.create(auth, new WriteContract(
                    "CT-RACE", "并发合同", "采购", "北方公司", new BigDecimal("1.00"),
                    "CNY", null, null, null, null)).id();
            contractId = createdId;

            CountDownLatch ready = new CountDownLatch(2);
            CountDownLatch start = new CountDownLatch(1);
            ExecutorService pool = Executors.newFixedThreadPool(2);
            try {
                Future<ContractVersionView> left = pool.submit(
                        () -> create(auth, createdId, first.id(), "并发一", ready, start));
                Future<ContractVersionView> right = pool.submit(
                        () -> create(auth, createdId, second.id(), "并发二", ready, start));
                assertTrue(ready.await(10, TimeUnit.SECONDS));
                start.countDown();
                ContractVersionView firstResult = left.get(20, TimeUnit.SECONDS);
                ContractVersionView secondResult = right.get(20, TimeUnit.SECONDS);
                Set<Integer> numbers = Set.of(firstResult.versionNo(), secondResult.versionNo());
                assertEquals(Set.of(1, 2), numbers);
            } finally {
                pool.shutdownNow();
            }

            UUID createdContract = contractId;
            List<VersionRow> rows = new TransactionTemplate(transactionManager).execute(status -> {
                jdbc.queryForObject(
                        "select set_config('app.tenant_id', ?, true)", String.class, tenant.id().toString());
                return jdbc.query("""
                        select version_no, is_current
                        from business.contract_version
                        where contract_id = ?
                        order by version_no
                        """, (rs, rowNum) -> new VersionRow(rs.getInt(1), rs.getBoolean(2)), createdContract);
            });
            assertEquals(List.of(1, 2), rows.stream().map(VersionRow::versionNo).toList());
            assertEquals(1, rows.stream().filter(VersionRow::current).count());
            assertEquals(Set.of(1, 2), rows.stream().map(VersionRow::versionNo).collect(Collectors.toSet()));
        } finally {
            TenantContext.clear();
            cleanup(tenantId, contractId, fileIds);
        }
    }

    private ContractVersionView create(
            AuthenticatedUser user, UUID contractId, UUID fileId, String summary,
            CountDownLatch ready, CountDownLatch start) throws Exception {
        TenantContext.set(user.tenantId());
        try {
            ready.countDown();
            if (!start.await(10, TimeUnit.SECONDS)) {
                throw new IllegalStateException("并发开始超时");
            }
            return contracts.createVersion(user, contractId, new CreateVersion(fileId, summary));
        } finally {
            TenantContext.clear();
        }
    }

    private void cleanup(UUID tenantId, UUID contractId, List<UUID> fileIds) {
        if (tenantId == null) {
            return;
        }
        new TransactionTemplate(transactionManager).executeWithoutResult(status -> {
            jdbc.queryForObject("select set_config('app.tenant_id', ?, true)", String.class, tenantId.toString());
            List<String> keys = jdbc.query(
                    "select object_key from business.stored_file where tenant_id = ?",
                    (rs, rowNum) -> rs.getString(1), tenantId);
            for (String key : keys) {
                storage.delete(properties.getBucket(), key);
            }
            if (contractId != null) {
                jdbc.update("delete from business.contract_version where tenant_id = ? and contract_id = ?",
                        tenantId, contractId);
                jdbc.update("delete from business.contract where tenant_id = ? and id = ?", tenantId, contractId);
            }
            for (UUID fileId : fileIds) {
                jdbc.update("delete from business.stored_file where tenant_id = ? and id = ?", tenantId, fileId);
            }
            jdbc.update("delete from business.user_role where tenant_id = ?", tenantId);
            jdbc.update("delete from business.role_permission where tenant_id = ?", tenantId);
            jdbc.update("delete from business.role where tenant_id = ?", tenantId);
            jdbc.update("delete from business.app_user where tenant_id = ?", tenantId);
            jdbc.update("delete from business.tenant where id = ?", tenantId);
        });
    }

    private record VersionRow(int versionNo, boolean current) {
    }
}
