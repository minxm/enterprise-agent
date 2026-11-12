package com.example.enterprise.file;

import org.junit.jupiter.api.Test;

import java.nio.charset.StandardCharsets;
import java.time.LocalDate;
import java.util.UUID;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;
import java.io.ByteArrayOutputStream;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class FileRulesTest {

    @Test
    void acceptsPdfDocxDocAndTextByContent() throws Exception {
        assertEquals("pdf", FileTypes.detect("C:\\incoming\\合同.PDF", "%PDF-1.4\n".getBytes(StandardCharsets.US_ASCII)).extension());
        assertEquals("docx", FileTypes.detect("clause.docx", docx()).extension());
        assertEquals("doc", FileTypes.detect("clause.doc", ole()).extension());
        assertEquals("txt", FileTypes.detect("note.txt", "条款\n".getBytes(StandardCharsets.UTF_8)).extension());
    }

    @Test
    void rejectsExtensionThatDoesNotMatchContent() {
        assertThrows(FileException.class, () -> FileTypes.detect("note.pdf", "hello".getBytes(StandardCharsets.UTF_8)));
        assertThrows(FileException.class, () -> FileTypes.detect("note.exe", "%PDF-1.4\n".getBytes(StandardCharsets.US_ASCII)));
        assertThrows(FileException.class, () -> FileTypes.detect("note.txt", "%PDF-1.4\n".getBytes(StandardCharsets.US_ASCII)));
    }

    @Test
    void displayNameDropsPathAndObjectKeyIgnoresIt() {
        assertEquals("evil.pdf", FileNames.displayName("..\\..\\incoming\\evil.pdf", "pdf"));
        UUID tenantId = UUID.fromString("11111111-1111-1111-1111-111111111111");
        UUID fileId = UUID.fromString("22222222-2222-2222-2222-222222222222");
        String key = ObjectKeys.create(tenantId, fileId, "pdf", LocalDate.of(2026, 11, 10));
        assertEquals("11111111-1111-1111-1111-111111111111/files/2026/11/22222222-2222-2222-2222-222222222222.pdf", key);
        assertFalse(key.contains("evil"));
        assertFalse(key.contains(".."));
        assertTrue(FileNames.contentDisposition("evil.pdf").startsWith("attachment;"));
    }

    private static byte[] docx() throws Exception {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        try (ZipOutputStream zip = new ZipOutputStream(output)) {
            zip.putNextEntry(new ZipEntry("word/document.xml"));
            zip.write("<document/>".getBytes(StandardCharsets.UTF_8));
            zip.closeEntry();
        }
        return output.toByteArray();
    }

    private static byte[] ole() {
        byte[] content = new byte[] {
                (byte) 0xD0, (byte) 0xCF, (byte) 0x11, (byte) 0xE0,
                (byte) 0xA1, (byte) 0xB1, (byte) 0x1A, (byte) 0xE1,
                0, 0, 0, 0
        };
        return content;
    }
}
