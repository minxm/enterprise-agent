package com.example.enterprise.file;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

public final class FileViews {

    private FileViews() {
    }

    public record FileView(
            UUID id,
            String filename,
            String mediaType,
            long byteSize,
            String extension,
            String checksumSha256,
            String status,
            UUID uploadedBy,
            Instant createdAt) {
    }

    public record FilePage(List<FileView> items, int page, int size, long total) {
    }

    public record PendingDownload(FileView file, String bucket, String objectKey) {
    }
}
