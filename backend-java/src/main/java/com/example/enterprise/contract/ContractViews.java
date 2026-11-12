package com.example.enterprise.contract;

import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;
import java.util.List;
import java.util.UUID;

public final class ContractViews {

    private ContractViews() {
    }

    public record ContractFileView(
            UUID fileId,
            String fileRole,
            String filename,
            String mediaType,
            long byteSize,
            String extension,
            String checksumSha256,
            String fileStatus,
            Instant linkedAt) {
    }

    public record ContractView(
            UUID id,
            String contractNo,
            String title,
            String contractType,
            String counterpartyName,
            BigDecimal amount,
            String currency,
            LocalDate signDate,
            LocalDate effectiveDate,
            LocalDate expiryDate,
            String status,
            String description,
            UUID createdBy,
            UUID updatedBy,
            Instant createdAt,
            Instant updatedAt,
            List<ContractFileView> files) {
    }

    public record ContractPage(List<ContractView> items, int page, int size, long total) {
    }

    public record WriteContract(
            String contractNo,
            String title,
            String contractType,
            String counterpartyName,
            BigDecimal amount,
            String currency,
            LocalDate signDate,
            LocalDate effectiveDate,
            LocalDate expiryDate,
            String description) {
    }

    public record StatusChange(String status) {
    }

    public record LinkFile(UUID fileId, String fileRole) {
    }

    public record CreateVersion(UUID fileId, String changeSummary) {
    }

    public record ContractVersionView(
            UUID versionId,
            int versionNo,
            String versionLabel,
            UUID fileId,
            String filename,
            String mediaType,
            long byteSize,
            String extension,
            String checksumSha256,
            String fileStatus,
            String changeSummary,
            boolean current,
            UUID createdBy,
            Instant createdAt) {
    }
}
