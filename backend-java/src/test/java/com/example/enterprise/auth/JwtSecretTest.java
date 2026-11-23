package com.example.enterprise.auth;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.example.enterprise.EnterpriseApplication;
import java.time.Clock;
import java.time.Duration;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.boot.builder.SpringApplicationBuilder;

class JwtSecretTest {

    @Test
    void shortSecretIsRejected() {
        assertThatThrownBy(() -> new JwtService("short-secret", "enterprise-agent", Duration.ofHours(2), Clock.systemUTC()))
                .isInstanceOf(IllegalStateException.class)
                .hasMessage("JWT 签名密钥未配置或长度不足");
    }

    @Test
    @EnabledIfEnvironmentVariable(named = "BUSINESS_APP_PASSWORD", matches = ".+")
    void prodDoesNotStartWhenTheSecretIsBlankOrShort() {
        assertRejected("--enterprise.auth.secret=");
        assertRejected("--enterprise.auth.secret=short");
    }

    private static void assertRejected(String secretArgument) {
        assertThatThrownBy(() -> new SpringApplicationBuilder(EnterpriseApplication.class)
                .profiles("prod")
                .properties(
                        "MINIO_ENDPOINT=http://127.0.0.1:9000",
                        "MINIO_ACCESS_KEY=prod-profile-test-access",
                        "MINIO_SECRET_KEY=prod-profile-test-secret",
                        "AGENT_SERVICE_BASE_URL=http://127.0.0.1:8000")
                .run(secretArgument, "--spring.main.web-application-type=none"))
                .satisfies(error -> assertThat(messages(error)).contains("JWT 签名密钥未配置或长度不足"));
    }

    private static String messages(Throwable error) {
        StringBuilder builder = new StringBuilder();
        Throwable current = error;
        while (current != null) {
            if (current.getMessage() != null) {
                builder.append(current.getMessage()).append('\n');
            }
            current = current.getCause();
        }
        return builder.toString();
    }
}
