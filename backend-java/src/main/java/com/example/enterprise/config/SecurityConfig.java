package com.example.enterprise.config;

import com.example.enterprise.auth.AuthProperties;
import com.example.enterprise.auth.AuthService;
import com.example.enterprise.auth.BearerTokenFilter;
import com.example.enterprise.auth.JwtService;
import com.example.enterprise.common.response.ApiResponse;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Profile;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configurers.AbstractHttpConfigurer;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.UsernamePasswordAuthenticationFilter;

import java.nio.charset.StandardCharsets;
import java.time.Clock;

@Configuration
@EnableConfigurationProperties(AuthProperties.class)
public class SecurityConfig {

    @Bean
    public SecurityFilterChain securityFilterChain(
            HttpSecurity http,
            ObjectProvider<JwtService> tokens,
            ObjectProvider<AuthService> auth,
            ObjectMapper objectMapper) throws Exception {
        JwtService jwtService = tokens.getIfAvailable();
        AuthService authService = auth.getIfAvailable();
        if (jwtService != null && authService != null) {
            http.addFilterBefore(new BearerTokenFilter(jwtService, authService), UsernamePasswordAuthenticationFilter.class);
        }
        return http
                .csrf(AbstractHttpConfigurer::disable)
                .httpBasic(AbstractHttpConfigurer::disable)
                .formLogin(AbstractHttpConfigurer::disable)
                .anonymous(AbstractHttpConfigurer::disable)
                .sessionManagement(session -> session.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
                .exceptionHandling(exceptions -> exceptions.authenticationEntryPoint((request, response, authException) -> {
                    response.setStatus(401);
                    response.setCharacterEncoding(StandardCharsets.UTF_8.name());
                    response.setContentType(MediaType.APPLICATION_JSON_VALUE);
                    objectMapper.writeValue(response.getOutputStream(), ApiResponse.failure(401, "未认证"));
                }))
                .authorizeHttpRequests(requests -> requests
                        .requestMatchers("/actuator/health", "/actuator/info").permitAll()
                        .requestMatchers("/api/v1/integration/agent/health").permitAll()
                        .requestMatchers("/api/v1/test/**").permitAll()
                        .requestMatchers(HttpMethod.POST, "/api/auth/login").permitAll()
                        .requestMatchers("/api/v1/identity/**").authenticated()
                        .requestMatchers("/api/v1/files/**").authenticated()
                        .anyRequest().permitAll())
                .build();
    }

    @Bean
    @Profile("dev")
    public JwtService jwtService(AuthProperties properties) {
        return new JwtService(properties.getSecret(), properties.getIssuer(), properties.getTtl(), Clock.systemUTC());
    }
}
