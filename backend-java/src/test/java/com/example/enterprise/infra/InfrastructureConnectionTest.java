package com.example.enterprise.infra;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.data.redis.connection.RedisConnection;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.test.context.ActiveProfiles;

import javax.sql.DataSource;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.Statement;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

@SpringBootTest
@ActiveProfiles("dev")
@EnabledIfEnvironmentVariable(named = "BUSINESS_APP_PASSWORD", matches = ".+")
class InfrastructureConnectionTest {

    @Autowired
    private DataSource dataSource;

    @Autowired
    private StringRedisTemplate redis;

    @Test
    void businessAppConnectsToPostgresAndRedis() throws Exception {
        try (Connection connection = dataSource.getConnection();
             Statement statement = connection.createStatement()) {
            try (ResultSet user = statement.executeQuery("select current_user, current_schema()")) {
                assertTrue(user.next());
                assertEquals("business_app", user.getString(1));
                assertEquals("business", user.getString(2));
            }
            try (ResultSet vector = statement.executeQuery(
                    "select extversion from pg_extension where extname = 'vector'")) {
                assertTrue(vector.next(), "vector extension is missing");
                assertFalse(vector.getString(1).isBlank());
            }
            assertFalse(privilege(statement, "agent"));
            assertTrue(privilege(statement, "business"));
            try (ResultSet history = statement.executeQuery(
                    "select to_regclass('business.flyway_schema_history') is not null")) {
                assertTrue(history.next());
                assertTrue(history.getBoolean(1));
            }
            try (ResultSet contract = statement.executeQuery(
                    "select to_regclass('business.contract') is not null")) {
                assertTrue(contract.next());
                assertTrue(contract.getBoolean(1));
            }
        }

        try (RedisConnection connection = redis.getConnectionFactory().getConnection()) {
            assertEquals("PONG", connection.ping());
        }
    }

    private static boolean privilege(Statement statement, String schema) throws Exception {
        try (ResultSet result = statement.executeQuery(
                "select has_schema_privilege(current_user, '" + schema + "', 'USAGE')")) {
            assertTrue(result.next());
            return result.getBoolean(1);
        }
    }
}
