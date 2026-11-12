package com.example.enterprise.file;

import com.example.enterprise.file.FileViews.FileView;
import org.springframework.context.annotation.Profile;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

@Repository
@Profile("dev")
public class StoredFileRepository {

    private final JdbcTemplate jdbc;

    public StoredFileRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public void applyTenant(UUID tenantId) {
        jdbc.queryForObject("select set_config('app.tenant_id', ?, true)", String.class, tenantId.toString());
    }

    public void insert(StoredFileRow row) {
        jdbc.update("""
                insert into business.stored_file (
                    id, tenant_id, filename, object_key, bucket_name, media_type,
                    byte_size, extension, checksum_sha256, uploaded_by, status)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, row.id(), row.tenantId(), row.filename(), row.objectKey(), row.bucketName(),
                row.mediaType(), row.byteSize(), row.extension(), row.checksumSha256(),
                row.uploadedBy(), row.status());
    }

    public Optional<StoredFileRow> find(UUID tenantId, UUID id) {
        return jdbc.query("""
                select id, tenant_id, filename, object_key, bucket_name, media_type,
                       byte_size, extension, checksum_sha256, uploaded_by, status, created_at
                from business.stored_file
                where id = ? and tenant_id = ?
                """, (rs, rowNum) -> new StoredFileRow(
                rs.getObject("id", UUID.class),
                rs.getObject("tenant_id", UUID.class),
                rs.getString("filename"),
                rs.getString("object_key"),
                rs.getString("bucket_name"),
                rs.getString("media_type"),
                rs.getLong("byte_size"),
                rs.getString("extension"),
                rs.getString("checksum_sha256"),
                rs.getObject("uploaded_by", UUID.class),
                rs.getString("status"),
                rs.getObject("created_at", OffsetDateTime.class).toInstant()), id, tenantId).stream().findFirst();
    }

    public List<FileView> listReady(UUID tenantId, int limit, int offset) {
        return jdbc.query("""
                select id, tenant_id, filename, object_key, bucket_name, media_type,
                       byte_size, extension, checksum_sha256, uploaded_by, status, created_at
                from business.stored_file
                where tenant_id = ? and status = 'ready'
                order by created_at desc
                limit ? offset ?
                """, (rs, rowNum) -> toView(map(rs)), tenantId, limit, offset);
    }

    public long countReady(UUID tenantId) {
        Long count = jdbc.queryForObject("""
                select count(*)
                from business.stored_file
                where tenant_id = ? and status = 'ready'
                """, Long.class, tenantId);
        return count == null ? 0 : count;
    }

    public boolean markDeleted(UUID tenantId, UUID id) {
        return jdbc.update("""
                update business.stored_file
                set status = 'deleted', updated_at = now()
                where id = ? and tenant_id = ? and status = 'ready'
                """, id, tenantId) == 1;
    }

    static FileView toView(StoredFileRow row) {
        return new FileView(
                row.id(),
                row.filename(),
                row.mediaType(),
                row.byteSize(),
                row.extension(),
                row.checksumSha256(),
                row.status(),
                row.uploadedBy(),
                row.createdAt());
    }

    private static StoredFileRow map(java.sql.ResultSet rs) throws java.sql.SQLException {
        return new StoredFileRow(
                rs.getObject("id", UUID.class),
                rs.getObject("tenant_id", UUID.class),
                rs.getString("filename"),
                rs.getString("object_key"),
                rs.getString("bucket_name"),
                rs.getString("media_type"),
                rs.getLong("byte_size"),
                rs.getString("extension"),
                rs.getString("checksum_sha256"),
                rs.getObject("uploaded_by", UUID.class),
                rs.getString("status"),
                rs.getObject("created_at", OffsetDateTime.class).toInstant());
    }

    public record StoredFileRow(
            UUID id,
            UUID tenantId,
            String filename,
            String objectKey,
            String bucketName,
            String mediaType,
            long byteSize,
            String extension,
            String checksumSha256,
            UUID uploadedBy,
            String status,
            java.time.Instant createdAt) {
    }
}
