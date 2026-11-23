package com.example.enterprise.config;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.fail;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.sql.Savepoint;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;

@EnabledIfEnvironmentVariable(named = "BUSINESS_APP_PASSWORD", matches = ".+")
class FlywayHistoryPrivilegeTest {

    @Test
    void businessAppCanReadFlywayHistoryAndCannotChangeIt() throws Exception {
        String url = "jdbc:postgresql://" + System.getenv("POSTGRES_HOST") + ":"
                + System.getenv("POSTGRES_PORT") + "/" + System.getenv("POSTGRES_DB");
        try (Connection connection = DriverManager.getConnection(
                url, "business_app", System.getenv("BUSINESS_APP_PASSWORD"))) {
            connection.setAutoCommit(false);
            try (var statement = connection.createStatement()) {
                try (var user = statement.executeQuery(
                        "select current_user, (select rolsuper from pg_roles where rolname = current_user)")) {
                    user.next();
                    assertEquals("business_app", user.getString(1));
                    assertFalse(user.getBoolean(2));
                }
                try (var versions = statement.executeQuery(
                        "select version from business.flyway_schema_history where success order by installed_rank")) {
                    boolean sawVersion = false;
                    while (versions.next()) {
                        sawVersion = true;
                    }
                    if (!sawVersion) {
                        fail("business_app could not read a Flyway version");
                    }
                }
                assertDenied(connection, """
                        insert into business.flyway_schema_history
                            (installed_rank, version, description, type, script, checksum, installed_by, execution_time, success)
                        values (999, '999', 'probe', 'SQL', 'probe.sql', 0, 'probe', 0, true)
                        """);
                assertDenied(connection, """
                        update business.flyway_schema_history
                           set description = description
                         where installed_rank = 999
                        """);
                assertDenied(connection, """
                        delete from business.flyway_schema_history where installed_rank = 999
                        """);
                assertDenied(connection, "create table business.day48_privilege_probe (id int)");
                assertDenied(connection, "alter table business.flyway_schema_history add column day48_probe int");
                assertDenied(connection, "truncate table business.flyway_schema_history");
                assertDenied(connection, "drop table business.flyway_schema_history");
            } finally {
                connection.rollback();
            }
        }
    }

    private static void assertDenied(Connection connection, String sql) throws SQLException {
        Savepoint savepoint = connection.setSavepoint();
        try (var statement = connection.createStatement()) {
            statement.execute(sql);
            fail("business_app was allowed to run: " + sql);
        } catch (SQLException ex) {
            assertEquals("42501", ex.getSQLState(), sql);
            connection.rollback(savepoint);
        }
    }
}
