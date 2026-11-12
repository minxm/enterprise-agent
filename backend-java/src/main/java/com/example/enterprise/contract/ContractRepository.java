package com.example.enterprise.contract;

import com.example.enterprise.contract.ContractViews.ContractFileView;
import com.example.enterprise.contract.ContractViews.ContractVersionView;
import com.example.enterprise.contract.ContractViews.ContractView;
import org.springframework.context.annotation.Profile;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.ConnectionCallback;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Savepoint;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import java.util.function.Supplier;

@Repository
@Profile("dev")
public class ContractRepository {

    private final JdbcTemplate jdbc;

    public ContractRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public void applyTenant(UUID tenantId) {
        jdbc.queryForObject("select set_config('app.tenant_id', ?, true)", String.class, tenantId.toString());
    }

    public void insert(ContractRow row) {
        write(() -> jdbc.update("""
                insert into business.contract (
                    id, tenant_id, contract_no, title, contract_type, counterparty_name,
                    amount, currency, sign_date, effective_date, expiry_date, status,
                    description, created_by, updated_by)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, row.id(), row.tenantId(), row.contractNo(), row.title(), row.contractType(),
                row.counterpartyName(), row.amount(), row.currency(), row.signDate(), row.effectiveDate(),
                row.expiryDate(), row.status(), row.description(), row.createdBy(), row.updatedBy()));
    }

    public boolean update(ContractRow row) {
        return write(() -> jdbc.update("""
                update business.contract
                set contract_no = ?, title = ?, contract_type = ?, counterparty_name = ?,
                    amount = ?, currency = ?, sign_date = ?, effective_date = ?, expiry_date = ?,
                    description = ?, updated_by = ?, updated_at = now()
                where id = ? and tenant_id = ? and status in ('draft', 'rejected')
                """, row.contractNo(), row.title(), row.contractType(), row.counterpartyName(),
                row.amount(), row.currency(), row.signDate(), row.effectiveDate(), row.expiryDate(),
                row.description(), row.updatedBy(), row.id(), row.tenantId()) == 1);
    }

    public boolean updateStatus(UUID tenantId, UUID id, String from, String to, UUID updatedBy) {
        return jdbc.update("""
                update business.contract
                set status = ?, updated_by = ?, updated_at = now()
                where id = ? and tenant_id = ? and status = ?
                """, to, updatedBy, id, tenantId, from) == 1;
    }

    public Optional<ContractRow> find(UUID tenantId, UUID id) {
        return jdbc.query(selectContract() + " where id = ? and tenant_id = ?",
                (rs, rowNum) -> mapContract(rs), id, tenantId).stream().findFirst();
    }

    public List<ContractView> list(ContractQuery query, int limit, int offset) {
        StringBuilder sql = new StringBuilder(selectContract()).append(" where tenant_id = ?");
        List<Object> args = new ArrayList<>();
        args.add(query.tenantId());
        appendFilters(sql, args, query);
        sql.append(" order by created_at desc limit ? offset ?");
        args.add(limit);
        args.add(offset);
        return jdbc.query(sql.toString(), (rs, rowNum) -> toView(mapContract(rs), List.of()), args.toArray());
    }

    public long count(ContractQuery query) {
        StringBuilder sql = new StringBuilder("select count(*) from business.contract where tenant_id = ?");
        List<Object> args = new ArrayList<>();
        args.add(query.tenantId());
        appendFilters(sql, args, query);
        Long count = jdbc.queryForObject(sql.toString(), Long.class, args.toArray());
        return count == null ? 0 : count;
    }

    public void linkFile(UUID id, UUID tenantId, UUID contractId, UUID fileId, String fileRole, UUID createdBy) {
        write(() -> jdbc.update("""
                insert into business.contract_file (id, tenant_id, contract_id, file_id, file_role, created_by)
                values (?, ?, ?, ?, ?, ?)
                """, id, tenantId, contractId, fileId, fileRole, createdBy));
    }

    public boolean lockContract(UUID tenantId, UUID contractId) {
        return !jdbc.query("""
                select id from business.contract
                where id = ? and tenant_id = ?
                for update
                """, (rs, rowNum) -> rs.getObject("id", UUID.class), contractId, tenantId).isEmpty();
    }

    public int nextVersionNo(UUID tenantId, UUID contractId) {
        Integer max = jdbc.queryForObject("""
                select coalesce(max(version_no), 0)
                from business.contract_version
                where tenant_id = ? and contract_id = ?
                """, Integer.class, tenantId, contractId);
        return (max == null ? 0 : max) + 1;
    }

    public void clearCurrentVersion(UUID tenantId, UUID contractId) {
        jdbc.update("""
                update business.contract_version
                set is_current = false
                where tenant_id = ? and contract_id = ? and is_current
                """, tenantId, contractId);
    }

    public void insertVersion(
            UUID id, UUID tenantId, UUID contractId, int versionNo, String versionLabel,
            UUID fileId, String changeSummary, UUID createdBy) {
        write(() -> jdbc.update("""
                insert into business.contract_version (
                    id, tenant_id, contract_id, version_no, version_label, file_id,
                    change_summary, is_current, created_by)
                values (?, ?, ?, ?, ?, ?, ?, true, ?)
                """, id, tenantId, contractId, versionNo, versionLabel, fileId, changeSummary, createdBy));
    }

    public Optional<ContractVersionView> findCurrentVersion(UUID tenantId, UUID contractId) {
        return queryVersions("""
                select %s
                from business.contract_version version
                join business.stored_file file
                  on file.id = version.file_id and file.tenant_id = version.tenant_id
                where version.tenant_id = ? and version.contract_id = ? and version.is_current
                """.formatted(versionColumns()), tenantId, contractId).stream().findFirst();
    }

    public Optional<ContractVersionView> findVersion(UUID tenantId, UUID contractId, UUID versionId) {
        return queryVersions("""
                select %s
                from business.contract_version version
                join business.stored_file file
                  on file.id = version.file_id and file.tenant_id = version.tenant_id
                where version.tenant_id = ? and version.contract_id = ? and version.id = ?
                """.formatted(versionColumns()), tenantId, contractId, versionId).stream().findFirst();
    }

    public List<ContractVersionView> listVersions(UUID tenantId, UUID contractId) {
        return queryVersions("""
                select %s
                from business.contract_version version
                join business.stored_file file
                  on file.id = version.file_id and file.tenant_id = version.tenant_id
                where version.tenant_id = ? and version.contract_id = ?
                order by version.version_no desc
                """.formatted(versionColumns()), tenantId, contractId);
    }

    public boolean unlinkFile(UUID tenantId, UUID contractId, UUID fileId) {
        return jdbc.update("""
                delete from business.contract_file
                where tenant_id = ? and contract_id = ? and file_id = ?
                """, tenantId, contractId, fileId) == 1;
    }

    public List<ContractFileView> listFiles(UUID tenantId, UUID contractId) {
        return jdbc.query("""
                select link.file_id, link.file_role, file.filename, file.media_type, file.byte_size,
                       file.extension, file.checksum_sha256, file.status, link.created_at
                from business.contract_file link
                join business.stored_file file
                  on file.id = link.file_id and file.tenant_id = link.tenant_id
                where link.tenant_id = ? and link.contract_id = ?
                order by link.created_at
                """, (rs, rowNum) -> new ContractFileView(
                rs.getObject("file_id", UUID.class),
                rs.getString("file_role"),
                rs.getString("filename"),
                rs.getString("media_type"),
                rs.getLong("byte_size"),
                rs.getString("extension"),
                rs.getString("checksum_sha256"),
                rs.getString("status"),
                rs.getObject("created_at", OffsetDateTime.class).toInstant()), tenantId, contractId);
    }

    static ContractView toView(ContractRow row, List<ContractFileView> files) {
        return new ContractView(
                row.id(), row.contractNo(), row.title(), row.contractType(), row.counterpartyName(),
                row.amount(), row.currency(), row.signDate(), row.effectiveDate(), row.expiryDate(),
                row.status(), row.description(), row.createdBy(), row.updatedBy(),
                row.createdAt(), row.updatedAt(), files);
    }

    private static void appendFilters(StringBuilder sql, List<Object> args, ContractQuery query) {
        if (query.contractNo() != null) {
            sql.append(" and contract_no = ?");
            args.add(query.contractNo());
        }
        if (query.titleLike() != null) {
            sql.append(" and title ilike ? escape '\\'");
            args.add(query.titleLike());
        }
        if (query.counterpartyLike() != null) {
            sql.append(" and counterparty_name ilike ? escape '\\'");
            args.add(query.counterpartyLike());
        }
        if (query.contractType() != null) {
            sql.append(" and contract_type = ?");
            args.add(query.contractType());
        }
        if (query.status() != null) {
            sql.append(" and status = ?");
            args.add(query.status());
        }
        if (query.signDateFrom() != null) {
            sql.append(" and sign_date >= ?");
            args.add(query.signDateFrom());
        }
        if (query.signDateTo() != null) {
            sql.append(" and sign_date <= ?");
            args.add(query.signDateTo());
        }
    }

    private static String selectContract() {
        return """
                select id, tenant_id, contract_no, title, contract_type, counterparty_name,
                       amount, currency, sign_date, effective_date, expiry_date, status,
                       description, created_by, updated_by, created_at, updated_at
                from business.contract
                """;
    }

    private static ContractRow mapContract(ResultSet rs) throws SQLException {
        return new ContractRow(
                rs.getObject("id", UUID.class),
                rs.getObject("tenant_id", UUID.class),
                rs.getString("contract_no"),
                rs.getString("title"),
                rs.getString("contract_type"),
                rs.getString("counterparty_name"),
                rs.getBigDecimal("amount"),
                rs.getString("currency"),
                rs.getObject("sign_date", LocalDate.class),
                rs.getObject("effective_date", LocalDate.class),
                rs.getObject("expiry_date", LocalDate.class),
                rs.getString("status"),
                rs.getString("description"),
                rs.getObject("created_by", UUID.class),
                rs.getObject("updated_by", UUID.class),
                rs.getObject("created_at", OffsetDateTime.class).toInstant(),
                rs.getObject("updated_at", OffsetDateTime.class).toInstant());
    }

    private <T> T write(Supplier<T> action) {
        return jdbc.execute((ConnectionCallback<T>) connection -> {
            Savepoint savepoint = connection.setSavepoint();
            try {
                T result = action.get();
                connection.releaseSavepoint(savepoint);
                return result;
            } catch (DataIntegrityViolationException exception) {
                connection.rollback(savepoint);
                throw mapped(exception);
            }
        });
    }

    private static ContractException mapped(DataIntegrityViolationException exception) {
        String message = String.valueOf(exception.getMostSpecificCause().getMessage());
        if (message.contains("contract_no_unique")) {
            return new ContractException(HttpStatus.CONFLICT, "合同编号已存在");
        }
        if (message.contains("contract_file_link_unique")) {
            return new ContractException(HttpStatus.CONFLICT, "文件已关联");
        }
        if (message.contains("contract_file_file_fk") || message.contains("contract_file_contract_fk")) {
            return new ContractException(HttpStatus.NOT_FOUND, "合同或文件不存在");
        }
        if (message.contains("contract_version_no_unique") || message.contains("contract_version_current_unique")) {
            return new ContractException(HttpStatus.CONFLICT, "版本冲突");
        }
        if (message.contains("contract_version_file_fk") || message.contains("contract_version_contract_fk")) {
            return new ContractException(HttpStatus.NOT_FOUND, "合同或文件不存在");
        }
        return new ContractException(HttpStatus.CONFLICT, "数据约束冲突");
    }

    private List<ContractVersionView> queryVersions(String sql, Object... args) {
        return jdbc.query(sql, (rs, rowNum) -> new ContractVersionView(
                rs.getObject("id", UUID.class),
                rs.getInt("version_no"),
                rs.getString("version_label"),
                rs.getObject("file_id", UUID.class),
                rs.getString("filename"),
                rs.getString("media_type"),
                rs.getLong("byte_size"),
                rs.getString("extension"),
                rs.getString("checksum_sha256"),
                rs.getString("status"),
                rs.getString("change_summary"),
                rs.getBoolean("is_current"),
                rs.getObject("created_by", UUID.class),
                rs.getObject("created_at", OffsetDateTime.class).toInstant()), args);
    }

    private static String versionColumns() {
        return """
                version.id, version.version_no, version.version_label, version.file_id,
                version.change_summary, version.is_current, version.created_by, version.created_at,
                file.filename, file.media_type, file.byte_size, file.extension,
                file.checksum_sha256, file.status
                """;
    }

    public record ContractRow(
            UUID id,
            UUID tenantId,
            String contractNo,
            String title,
            String contractType,
            String counterpartyName,
            java.math.BigDecimal amount,
            String currency,
            LocalDate signDate,
            LocalDate effectiveDate,
            LocalDate expiryDate,
            String status,
            String description,
            UUID createdBy,
            UUID updatedBy,
            java.time.Instant createdAt,
            java.time.Instant updatedAt) {
    }

    public record ContractQuery(
            UUID tenantId,
            String contractNo,
            String titleLike,
            String counterpartyLike,
            String contractType,
            String status,
            LocalDate signDateFrom,
            LocalDate signDateTo) {
    }
}
