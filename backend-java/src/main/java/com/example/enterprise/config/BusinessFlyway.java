package com.example.enterprise.config;

import java.sql.Connection;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.List;
import javax.sql.DataSource;
import org.flywaydb.core.Flyway;

final class BusinessFlyway {

    static final List<String> HISTORY_PRIVILEGE_STATEMENTS = List.of(
            "revoke all on table business.flyway_schema_history from business_app",
            "revoke all on table business.flyway_schema_history from public",
            "grant select on table business.flyway_schema_history to business_app");

    private BusinessFlyway() {
    }

    static Flyway open(String url, String username, String password) {
        if (url == null || url.isBlank() || !url.startsWith("jdbc:postgresql:")) {
            throw new IllegalStateException("迁移配置不完整: PostgreSQL 连接");
        }
        if (password == null || password.isBlank()) {
            throw new IllegalStateException("迁移配置不完整: BUSINESS_MIGRATOR_PASSWORD");
        }
        if (!"business_migrator".equals(username)) {
            throw new IllegalStateException("Flyway 必须使用 business_migrator");
        }
        return Flyway.configure()
                .dataSource(url, username, password)
                .schemas("business")
                .defaultSchema("business")
                .createSchemas(false)
                .locations("classpath:db/migration")
                .failOnMissingLocations(false)
                .load();
    }

    static void assertMigrator(Flyway flyway) {
        DataSource dataSource = flyway.getConfiguration().getDataSource();
        try (Connection connection = dataSource.getConnection();
             var statement = connection.createStatement();
             var user = statement.executeQuery(
                     "select current_user, (select rolsuper from pg_roles where rolname = current_user)")) {
            if (!user.next()) {
                throw new IllegalStateException("迁移账号不能是超级用户");
            }
            String currentUser = user.getString(1);
            boolean superuser = user.getBoolean(2);
            if (!"business_migrator".equals(currentUser)) {
                throw new IllegalStateException("Flyway 必须使用 business_migrator");
            }
            if (superuser) {
                throw new IllegalStateException("迁移账号不能是超级用户");
            }
        } catch (SQLException ex) {
            throw new IllegalStateException("数据库迁移失败: " + ex.getClass().getSimpleName(), ex);
        }
    }

    static int migrate(Flyway flyway) {
        assertMigrator(flyway);
        int executed = flyway.migrate().migrationsExecuted;
        restrictHistoryPrivileges(flyway);
        return executed;
    }

    static void restrictHistoryPrivileges(Flyway flyway) {
        DataSource dataSource = flyway.getConfiguration().getDataSource();
        try (Connection connection = dataSource.getConnection()) {
            connection.setAutoCommit(false);
            try (var statement = connection.createStatement()) {
                for (String sql : HISTORY_PRIVILEGE_STATEMENTS) {
                    statement.execute(sql);
                }
                try (var check = statement.executeQuery(
                        """
                        select has_table_privilege('business_app', 'business.flyway_schema_history', 'SELECT')
                           and not has_table_privilege('business_app', 'business.flyway_schema_history', 'INSERT')
                           and not has_table_privilege('business_app', 'business.flyway_schema_history', 'UPDATE')
                           and not has_table_privilege('business_app', 'business.flyway_schema_history', 'DELETE')
                           and not has_table_privilege('business_app', 'business.flyway_schema_history', 'TRUNCATE')
                        """)) {
                    if (!check.next() || !check.getBoolean(1)) {
                        throw new IllegalStateException("数据库迁移失败: business_app 仍可修改 Flyway 历史");
                    }
                }
            }
            connection.commit();
        } catch (SQLException ex) {
            throw new IllegalStateException("数据库迁移失败: " + ex.getClass().getSimpleName(), ex);
        }
    }

    static MigrationCheck check(String url, String username, String password) {
        Flyway flyway = open(url, username, password);
        try {
            int executed = migrate(flyway);
            try (Connection connection = flyway.getConfiguration().getDataSource().getConnection();
                 var statement = connection.createStatement()) {
                String currentUser;
                boolean superuser;
                try (var user = statement.executeQuery(
                        "select current_user, (select rolsuper from pg_roles where rolname = current_user)")) {
                    if (!user.next()) {
                        throw new IllegalStateException("Flyway 必须使用 business_migrator");
                    }
                    currentUser = user.getString(1);
                    superuser = user.getBoolean(2);
                }
                List<String> versions = new ArrayList<>();
                try (var rows = statement.executeQuery(
                        "select version from business.flyway_schema_history order by installed_rank")) {
                    while (rows.next()) {
                        versions.add(rows.getString(1));
                    }
                }
                long contracts;
                try (var count = statement.executeQuery("select count(*) from business.contract")) {
                    count.next();
                    contracts = count.getLong(1);
                }
                return new MigrationCheck(executed, currentUser, superuser, List.copyOf(versions), contracts);
            }
        } catch (SQLException ex) {
            throw new IllegalStateException("数据库迁移失败: " + ex.getClass().getSimpleName(), ex);
        } finally {
            close(flyway);
        }
    }

    static void close(Flyway flyway) {
        DataSource dataSource = flyway.getConfiguration().getDataSource();
        if (dataSource instanceof AutoCloseable closeable) {
            try {
                closeable.close();
            } catch (Exception ignored) {
                // 迁移进程即将退出，关闭失败不再覆盖原来的迁移结果。
            }
        }
    }

    record MigrationCheck(int executed, String user, boolean superuser, List<String> versions, long contracts) {
    }
}
