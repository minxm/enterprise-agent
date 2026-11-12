package com.example.enterprise.file;

import java.time.LocalDate;
import java.time.ZoneOffset;
import java.util.UUID;

final class ObjectKeys {

    private ObjectKeys() {
    }

    static String create(UUID tenantId, UUID fileId, String extension) {
        return create(tenantId, fileId, extension, LocalDate.now(ZoneOffset.UTC));
    }

    static String create(UUID tenantId, UUID fileId, String extension, LocalDate date) {
        return tenantId
                + "/files/"
                + date.getYear()
                + "/"
                + String.format("%02d", date.getMonthValue())
                + "/"
                + fileId
                + "."
                + extension;
    }
}
