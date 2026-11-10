package com.example.enterprise.infra;

import io.lettuce.core.ClientOptions;
import io.lettuce.core.SocketOptions;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.springframework.data.redis.connection.RedisStandaloneConfiguration;
import org.springframework.data.redis.connection.lettuce.LettuceClientConfiguration;
import org.springframework.data.redis.connection.lettuce.LettuceConnectionFactory;

import java.sql.DriverManager;
import java.sql.SQLException;
import java.time.Duration;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;

class InfrastructureFailureTest {

    @Test
    @Timeout(5)
    void unreachablePostgresReportsAnError() {
        SQLException exception = assertThrows(SQLException.class, () -> DriverManager.getConnection(
                "jdbc:postgresql://127.0.0.1:1/enterprise?connectTimeout=2",
                "business_app",
                "invalid"));
        assertNotNull(exception.getMessage());
        assertFalse(exception.getMessage().isBlank());
    }

    @Test
    @Timeout(5)
    void unreachableRedisReportsAnError() {
        RedisStandaloneConfiguration configuration = new RedisStandaloneConfiguration("127.0.0.1", 1);
        configuration.setPassword("invalid");
        SocketOptions socket = SocketOptions.builder().connectTimeout(Duration.ofMillis(800)).build();
        LettuceClientConfiguration client = LettuceClientConfiguration.builder()
                .clientOptions(ClientOptions.builder().socketOptions(socket).build())
                .commandTimeout(Duration.ofMillis(800))
                .build();
        LettuceConnectionFactory factory = new LettuceConnectionFactory(configuration, client);
        factory.afterPropertiesSet();
        try {
            Exception exception = assertThrows(Exception.class, () -> factory.getConnection().ping());
            assertNotNull(exception.getMessage());
            assertFalse(exception.getMessage().isBlank());
        } finally {
            factory.destroy();
        }
    }
}
