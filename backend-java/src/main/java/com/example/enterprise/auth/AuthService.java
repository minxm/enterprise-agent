package com.example.enterprise.auth;

import com.example.enterprise.identity.IdentityException;
import com.example.enterprise.identity.IdentityViews.TenantView;
import com.example.enterprise.identity.IdentityViews.UserView;
import com.example.enterprise.identity.PasswordHasher;
import org.springframework.context.annotation.Profile;
import org.springframework.http.HttpStatus;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.Optional;

@Service
@Profile("dev")
public class AuthService {

    private static final String LOGIN_FAILED = "登录失败";
    private static final String DUMMY_HASH = new BCryptPasswordEncoder().encode("dummy-password");

    private final AuthRepository repository;
    private final PasswordHasher passwords;
    private final JwtService tokens;

    public AuthService(AuthRepository repository, PasswordHasher passwords, JwtService tokens) {
        this.repository = repository;
        this.passwords = passwords;
        this.tokens = tokens;
    }

    @Transactional
    public LoginView login(String tenantCode, String username, String password) {
        if (isBlank(tenantCode) || isBlank(username) || isBlank(password)) {
            throw new IdentityException(HttpStatus.BAD_REQUEST, "请求参数不合法");
        }
        Optional<TenantView> tenant = repository.findTenantByCode(tenantCode.trim());
        if (tenant.isEmpty() || !"active".equals(tenant.get().status())) {
            passwords.matches(password, DUMMY_HASH);
            throw new IdentityException(HttpStatus.UNAUTHORIZED, LOGIN_FAILED);
        }
        repository.applyTenant(tenant.get().id());
        Optional<AuthRepository.StoredUser> user = repository.findUserByLogin(username.trim());
        String hash = user.map(AuthRepository.StoredUser::passwordHash).orElse(DUMMY_HASH);
        boolean passwordMatches = passwords.matches(password, hash);
        if (user.isEmpty() || !"active".equals(user.get().status()) || !passwordMatches) {
            throw new IdentityException(HttpStatus.UNAUTHORIZED, LOGIN_FAILED);
        }
        AuthRepository.StoredUser stored = user.get();
        return new LoginView(
                tokens.issue(stored.id(), stored.tenantId()),
                new UserView(stored.id(), stored.loginName(), stored.displayName(), stored.status()),
                tenant.get());
    }

    @Transactional
    public Optional<AuthenticatedUser> authenticate(AuthenticatedUser tokenUser) {
        Optional<TenantView> tenant = repository.findTenantById(tokenUser.tenantId());
        if (tenant.isEmpty() || !"active".equals(tenant.get().status())) {
            return Optional.empty();
        }
        repository.applyTenant(tokenUser.tenantId());
        Optional<AuthRepository.StoredUser> user = repository.findUserById(tokenUser.userId());
        if (user.isEmpty()
                || !"active".equals(user.get().status())
                || !user.get().tenantId().equals(tokenUser.tenantId())) {
            return Optional.empty();
        }
        return Optional.of(new AuthenticatedUser(user.get().id(), user.get().tenantId(), user.get().loginName()));
    }

    private static boolean isBlank(String value) {
        return value == null || value.isBlank();
    }
}
