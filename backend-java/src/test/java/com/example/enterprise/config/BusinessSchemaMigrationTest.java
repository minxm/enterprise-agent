package com.example.enterprise.config;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.sql.DriverManager;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.TestPropertySource;

@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.NONE)
@ActiveProfiles("migrate")
@TestPropertySource(properties = "enterprise.migration.exit=false")
@EnabledIfEnvironmentVariable(named = "BUSINESS_MIGRATOR_PASSWORD", matches = ".+")
class BusinessSchemaMigrationTest {

    @Autowired
    private BusinessSchemaMigration migration;

    @Test
    void repeatMigrationKeepsHistoryAndContracts() throws Exception {
        assertEquals(0, migration.executed());
        var first = BusinessFlyway.check(url(), "business_migrator", password());
        var second = BusinessFlyway.check(url(), "business_migrator", password());
        assertEquals(0, first.executed());
        assertEquals(0, second.executed());
        assertEquals("business_migrator", first.user());
        assertFalse(first.superuser());
        assertEquals(first.versions(), second.versions());
        assertEquals(first.contracts(), second.contracts());
        assertTrue(first.versions().containsAll(BusinessSchemaGuard.expectedVersions()));
        try (var connection = DriverManager.getConnection(url(), "business_migrator", password());
             var statement = connection.createStatement();
             var privileges = statement.executeQuery("""
                     select has_table_privilege('business_app', 'business.flyway_schema_history', 'SELECT'),
                            has_table_privilege('business_app', 'business.flyway_schema_history', 'INSERT'),
                            has_table_privilege('business_app', 'business.flyway_schema_history', 'UPDATE'),
                            has_table_privilege('business_app', 'business.flyway_schema_history', 'DELETE'),
                            has_table_privilege('business_app', 'business.flyway_schema_history', 'TRUNCATE')
                     """)) {
            assertTrue(privileges.next());
            assertTrue(privileges.getBoolean(1));
            assertFalse(privileges.getBoolean(2));
            assertFalse(privileges.getBoolean(3));
            assertFalse(privileges.getBoolean(4));
            assertFalse(privileges.getBoolean(5));
        }
    }

    @Test
    void concurrentMigrationsDoNotDiverge() throws Exception {
        var before = BusinessFlyway.check(url(), "business_migrator", password());
        var pool = Executors.newFixedThreadPool(2);
        try {
            var first = pool.submit(() -> BusinessFlyway.check(url(), "business_migrator", password()));
            var second = pool.submit(() -> BusinessFlyway.check(url(), "business_migrator", password()));
            assertEquals(before.versions(), first.get(30, TimeUnit.SECONDS).versions());
            assertEquals(before.contracts(), second.get(30, TimeUnit.SECONDS).contracts());
            assertEquals(0, first.get().executed());
            assertEquals(0, second.get().executed());
        } finally {
            pool.shutdownNow();
        }
    }

    private static String password() {
        return System.getenv("BUSINESS_MIGRATOR_PASSWORD");
    }

    private static String url() {
        return "jdbc:postgresql://" + System.getenv("POSTGRES_HOST") + ":"
                + System.getenv("POSTGRES_PORT") + "/" + System.getenv("POSTGRES_DB");
    }
}
