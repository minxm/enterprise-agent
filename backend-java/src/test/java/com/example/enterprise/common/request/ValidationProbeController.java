package com.example.enterprise.common.request;

import com.example.enterprise.common.response.ApiResponse;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class ValidationProbeController {

    @GetMapping("/api/v1/test/ping")
    public ApiResponse<String> ping() {
        return ApiResponse.success("pong");
    }

    @PostMapping("/api/v1/test/validation")
    public void validate(@Valid @RequestBody Sample body) {
    }

    public record Sample(@NotBlank String name) {
    }
}
