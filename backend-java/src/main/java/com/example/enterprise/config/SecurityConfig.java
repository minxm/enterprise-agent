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
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.UsernamePasswordAuthenticationFilter;

import java.nio.charset.StandardCharsets;
import java.time.Clock;

@Configuration
@Profile("!migrate")
@EnableConfigurationProperties(AuthProperties.class)
public class SecurityConfig {

    private static final Logger log = LoggerFactory.getLogger(SecurityConfig.class);

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
                .exceptionHandling(exceptions -> exceptions
                        .authenticationEntryPoint((request, response, authException) -> {
                            log.debug("rejected {} {}", request.getRequestURI(), authException.getClass().getSimpleName());
                            response.setStatus(401);
                            response.setCharacterEncoding(StandardCharsets.UTF_8.name());
                            response.setContentType(MediaType.APPLICATION_JSON_VALUE);
                            objectMapper.writeValue(response.getOutputStream(), ApiResponse.failure(401, "未认证"));
                        })
                        .accessDeniedHandler((request, response, denied) -> {
                            response.setStatus(403);
                            response.setCharacterEncoding(StandardCharsets.UTF_8.name());
                            response.setContentType(MediaType.APPLICATION_JSON_VALUE);
                            objectMapper.writeValue(response.getOutputStream(), ApiResponse.failure(403, "无权访问"));
                        }))
                .authorizeHttpRequests(requests -> requests
                        .requestMatchers("/actuator/health", "/actuator/info").permitAll()
                        .requestMatchers("/actuator/**").denyAll()
                        .requestMatchers("/api/v1/integration/agent/health").permitAll()
                        .requestMatchers(HttpMethod.POST, "/api/auth/login").permitAll()
                        .requestMatchers("/internal/v1/**").permitAll()
                        .anyRequest().authenticated())
                .build();
    }

    @Bean
    @Profile({"dev", "prod"})
    public JwtService jwtService(AuthProperties properties) {
        return new JwtService(properties.getSecret(), properties.getIssuer(), properties.getTtl(), Clock.systemUTC());
    }
}
