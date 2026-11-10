package com.example.enterprise.auth;

import com.example.enterprise.identity.TenantContext;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.util.List;

public class BearerTokenFilter extends OncePerRequestFilter {

    private final JwtService tokens;
    private final AuthService auth;

    public BearerTokenFilter(JwtService tokens, AuthService auth) {
        this.tokens = tokens;
        this.auth = auth;
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain filterChain)
            throws ServletException, IOException {
        String header = request.getHeader("Authorization");
        if (header != null && header.startsWith("Bearer ")) {
            String token = header.substring("Bearer ".length()).trim();
            tokens.parse(token)
                    .flatMap(auth::authenticate)
                    .ifPresent(user -> {
                        var authentication = UsernamePasswordAuthenticationToken.authenticated(user, null, List.of());
                        SecurityContextHolder.getContext().setAuthentication(authentication);
                        TenantContext.set(user.tenantId());
                    });
        }
        filterChain.doFilter(request, response);
    }
}
