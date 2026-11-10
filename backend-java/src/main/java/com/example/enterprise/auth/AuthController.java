package com.example.enterprise.auth;

import com.example.enterprise.common.response.ApiResponse;
import org.springframework.context.annotation.Profile;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@Profile("dev")
@RequestMapping("/api/auth")
public class AuthController {

    private final AuthService auth;

    public AuthController(AuthService auth) {
        this.auth = auth;
    }

    @PostMapping("/login")
    public ApiResponse<LoginView> login(@RequestBody LoginRequest request) {
        return ApiResponse.success(auth.login(request.tenantCode(), request.username(), request.password()));
    }

    public record LoginRequest(String tenantCode, String username, String password) {
    }
}
