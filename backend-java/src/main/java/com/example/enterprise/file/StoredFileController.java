package com.example.enterprise.file;

import com.example.enterprise.auth.AuthenticatedUser;
import com.example.enterprise.common.response.ApiResponse;
import com.example.enterprise.file.FileViews.FilePage;
import com.example.enterprise.file.FileViews.FileView;
import com.example.enterprise.file.FileViews.PendingDownload;
import org.springframework.context.annotation.Profile;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;
import org.springframework.web.servlet.mvc.method.annotation.StreamingResponseBody;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.util.UUID;

@RestController
@Profile("dev")
@RequestMapping("/api/v1/files")
public class StoredFileController {

    private final StoredFileService files;

    public StoredFileController(StoredFileService files) {
        this.files = files;
    }

    @PostMapping(value = "/upload", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public ApiResponse<FileView> upload(@RequestParam("file") MultipartFile file) throws IOException {
        return ApiResponse.success(files.upload(currentUser(), file.getOriginalFilename(), file.getBytes()));
    }

    @GetMapping
    public ApiResponse<FilePage> list(
            @RequestParam(defaultValue = "0") int page,
            @RequestParam(defaultValue = "20") int size) {
        return ApiResponse.success(files.list(page, size));
    }

    @GetMapping("/{fileId}")
    public ApiResponse<FileView> get(@PathVariable UUID fileId) {
        return ApiResponse.success(files.get(fileId));
    }

    @GetMapping("/{fileId}/download")
    public ResponseEntity<StreamingResponseBody> download(@PathVariable UUID fileId) {
        PendingDownload pending = files.open(fileId);
        StreamingResponseBody body = output -> {
            try {
                files.write(pending, output);
            } catch (UncheckedIOException exception) {
                throw exception;
            }
        };
        return ResponseEntity.ok()
                .contentType(MediaType.parseMediaType(pending.file().mediaType()))
                .contentLength(pending.file().byteSize())
                .header(HttpHeaders.CONTENT_DISPOSITION, FileNames.contentDisposition(pending.file().filename()))
                .body(body);
    }

    @DeleteMapping("/{fileId}")
    public ApiResponse<FileView> delete(@PathVariable UUID fileId) {
        return ApiResponse.success(files.delete(fileId));
    }

    private static AuthenticatedUser currentUser() {
        Authentication authentication = SecurityContextHolder.getContext().getAuthentication();
        if (authentication == null || !(authentication.getPrincipal() instanceof AuthenticatedUser user)) {
            throw new FileException(HttpStatus.UNAUTHORIZED, "未认证");
        }
        return user;
    }
}
