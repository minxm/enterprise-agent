package com.example.enterprise.file;

import com.example.enterprise.auth.AuthenticatedUser;
import com.example.enterprise.identity.TenantContext;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.context.SecurityContextHolder;

import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.argThat;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;

class StoredFileServiceTest {

    private final StoredFileRepository repository = mock(StoredFileRepository.class);
    private final ObjectStorage storage = mock(ObjectStorage.class);

    @AfterEach
    void cleanup() {
        SecurityContextHolder.clearContext();
        TenantContext.clear();
    }

    @Test
    void emptyAndOversizedAndUnknownFilesAreRejectedBeforeStorage() {
        StoredFileService service = service(8);
        authenticate();

        FileException empty = assertThrows(FileException.class, () -> service.upload(user(), "a.pdf", new byte[0]));
        FileException oversized = assertThrows(FileException.class, () -> service.upload(user(), "a.pdf", new byte[9]));
        FileException unknown = assertThrows(FileException.class,
                () -> service.upload(user(), "a.exe", "hello".getBytes(StandardCharsets.UTF_8)));

        assertEquals("文件不能为空", empty.getMessage());
        assertEquals("文件大小超出限制", oversized.getMessage());
        assertEquals("不支持的文件类型", unknown.getMessage());
        verify(storage, never()).upload(any(), any(), any(), any());
    }

    @Test
    void databaseFailureDeletesTheUploadedObject() {
        StoredFileService service = service(1024);
        authenticate();
        doThrow(new IllegalStateException("constraint")).when(repository).insert(any());

        FileException exception = assertThrows(FileException.class,
                () -> service.upload(user(), "..\\..\\evil.pdf", "%PDF-1.4\n".getBytes(StandardCharsets.US_ASCII)));

        assertEquals("文件保存失败", exception.getMessage());
        verify(storage).upload(org.mockito.ArgumentMatchers.eq("enterprise-agent"), argThat(key ->
                key.endsWith(".pdf") && !key.contains("evil") && !key.contains("..")), any(), org.mockito.ArgumentMatchers.eq(FileTypes.PDF));
        verify(storage).delete(org.mockito.ArgumentMatchers.eq("enterprise-agent"), argThat(key -> key.endsWith(".pdf")));
    }

    private StoredFileService service(long maxBytes) {
        StorageProperties properties = new StorageProperties();
        properties.setBucket("enterprise-agent");
        properties.setMaxBytes(maxBytes);
        return new StoredFileService(repository, storage, properties);
    }

    private void authenticate() {
        SecurityContextHolder.getContext().setAuthentication(UsernamePasswordAuthenticationToken.authenticated(
                user(), null, List.of()));
    }

    private static AuthenticatedUser user() {
        return new AuthenticatedUser(
                UUID.fromString("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                UUID.fromString("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
                "ada");
    }
}
