package com.example.enterprise.file;

import com.example.enterprise.auth.AuthenticatedUser;
import com.example.enterprise.file.FileViews.FilePage;
import com.example.enterprise.file.FileViews.FileView;
import com.example.enterprise.file.FileViews.PendingDownload;
import com.example.enterprise.file.StoredFileRepository.StoredFileRow;
import com.example.enterprise.identity.TenantContext;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.annotation.Lazy;
import org.springframework.context.annotation.Profile;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Instant;
import java.util.HexFormat;
import java.util.UUID;

@Service
@Profile("dev")
public class StoredFileService {

    private static final Logger log = LoggerFactory.getLogger(StoredFileService.class);

    private final StoredFileRepository repository;
    private final ObjectStorage storage;
    private final StorageProperties properties;

    public StoredFileService(StoredFileRepository repository, @Lazy ObjectStorage storage, StorageProperties properties) {
        this.repository = repository;
        this.storage = storage;
        this.properties = properties;
    }

    @Transactional
    public FileView upload(AuthenticatedUser user, String originalName, byte[] content) {
        UUID tenantId = TenantContext.require();
        if (!tenantId.equals(user.tenantId())) {
            throw new FileException(HttpStatus.FORBIDDEN, "租户上下文未建立");
        }
        if (content == null || content.length == 0) {
            throw new FileException(HttpStatus.BAD_REQUEST, "文件不能为空");
        }
        if (content.length > properties.getMaxBytes()) {
            throw new FileException(HttpStatus.BAD_REQUEST, "文件大小超出限制");
        }
        FileTypes.Detected detected = FileTypes.detect(originalName, content);
        String filename = FileNames.displayName(originalName, detected.extension());
        UUID fileId = UUID.randomUUID();
        String objectKey = ObjectKeys.create(tenantId, fileId, detected.extension());
        String checksum = sha256(content);
        try {
            storage.upload(properties.getBucket(), objectKey, content, detected.mediaType());
        } catch (RuntimeException exception) {
            log.warn("Object upload failed: {}", exception.getClass().getSimpleName());
            throw new FileException(HttpStatus.BAD_GATEWAY, "文件存储失败");
        }
        StoredFileRow row = new StoredFileRow(
                fileId,
                tenantId,
                filename,
                objectKey,
                properties.getBucket(),
                detected.mediaType(),
                content.length,
                detected.extension(),
                checksum,
                user.userId(),
                "ready",
                Instant.now());
        try {
            repository.applyTenant(tenantId);
            repository.insert(row);
        } catch (RuntimeException exception) {
            removeOrphan(objectKey);
            log.warn("File metadata insert failed: {}", exception.getClass().getSimpleName());
            throw new FileException(HttpStatus.INTERNAL_SERVER_ERROR, "文件保存失败");
        }
        return StoredFileRepository.toView(row);
    }

    @Transactional
    public FileView get(UUID fileId) {
        return StoredFileRepository.toView(requireReady(fileId));
    }

    @Transactional
    public FilePage list(int page, int size) {
        if (page < 0 || size < 1 || size > 100) {
            throw new FileException(HttpStatus.BAD_REQUEST, "请求参数不合法");
        }
        UUID tenantId = openTenant();
        return new FilePage(
                repository.listReady(tenantId, size, page * size),
                page,
                size,
                repository.countReady(tenantId));
    }

    @Transactional
    public PendingDownload open(UUID fileId) {
        StoredFileRow row = requireReady(fileId);
        return new PendingDownload(StoredFileRepository.toView(row), row.bucketName(), row.objectKey());
    }

    public void write(PendingDownload download, OutputStream output) {
        try (InputStream input = storage.download(download.bucket(), download.objectKey())) {
            input.transferTo(output);
        } catch (IOException exception) {
            throw new FileException(HttpStatus.BAD_GATEWAY, "文件读取失败");
        } catch (RuntimeException exception) {
            log.warn("Object download failed: {}", exception.getClass().getSimpleName());
            throw new FileException(HttpStatus.BAD_GATEWAY, "文件读取失败");
        }
    }

    @Transactional
    public FileView delete(UUID fileId) {
        UUID tenantId = openTenant();
        StoredFileRow row = repository.find(tenantId, fileId)
                .orElseThrow(() -> new FileException(HttpStatus.NOT_FOUND, "文件不存在"));
        if ("deleted".equals(row.status())) {
            return StoredFileRepository.toView(row);
        }
        if (!"ready".equals(row.status())) {
            throw new FileException(HttpStatus.NOT_FOUND, "文件不存在");
        }
        try {
            storage.delete(row.bucketName(), row.objectKey());
        } catch (RuntimeException exception) {
            log.warn("Object delete failed: {}", exception.getClass().getSimpleName());
            throw new FileException(HttpStatus.BAD_GATEWAY, "文件删除失败");
        }
        if (!repository.markDeleted(tenantId, fileId)) {
            throw new FileException(HttpStatus.INTERNAL_SERVER_ERROR, "文件删除未完成");
        }
        return new FileView(
                row.id(),
                row.filename(),
                row.mediaType(),
                row.byteSize(),
                row.extension(),
                row.checksumSha256(),
                "deleted",
                row.uploadedBy(),
                row.createdAt());
    }

    private StoredFileRow requireReady(UUID fileId) {
        UUID tenantId = openTenant();
        StoredFileRow row = repository.find(tenantId, fileId)
                .orElseThrow(() -> new FileException(HttpStatus.NOT_FOUND, "文件不存在"));
        if (!"ready".equals(row.status())) {
            throw new FileException(HttpStatus.NOT_FOUND, "文件不存在");
        }
        return row;
    }

    private UUID openTenant() {
        UUID tenantId = TenantContext.require();
        repository.applyTenant(tenantId);
        return tenantId;
    }

    private void removeOrphan(String objectKey) {
        try {
            storage.delete(properties.getBucket(), objectKey);
        } catch (RuntimeException exception) {
            log.warn("Orphan object cleanup failed: {}", exception.getClass().getSimpleName());
        }
    }

    private static String sha256(byte[] content) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(content));
        } catch (NoSuchAlgorithmException exception) {
            throw new IllegalStateException("SHA-256 不可用");
        }
    }
}
