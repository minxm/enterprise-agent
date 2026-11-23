package com.example.enterprise.config;

import java.io.IOException;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import javax.sql.DataSource;
import org.springframework.core.io.Resource;
import org.springframework.core.io.support.PathMatchingResourcePatternResolver;

final class BusinessSchemaGuard {

    private BusinessSchemaGuard() {
    }

    static SchemaCompatibility verify(DataSource dataSource) {
        Set<String> expected = expectedVersions();
        if (expected.isEmpty()) {
            throw new IllegalStateException("数据库未完成迁移");
        }
        try (Connection connection = dataSource.getConnection();
             var statement = connection.createStatement()) {
            try (var user = statement.executeQuery(
                    "select current_user, (select rolsuper from pg_roles where rolname = current_user)")) {
                if (!user.next() || !"business_app".equals(user.getString(1)) || user.getBoolean(2)) {
                    throw new IllegalStateException("业务连接必须使用 business_app");
                }
            }
            List<String> applied = new ArrayList<>();
            try (var versions = statement.executeQuery(
                    "select version from business.flyway_schema_history where success order by installed_rank")) {
                while (versions.next()) {
                    applied.add(versions.getString(1));
                }
            }
            if (!applied.containsAll(expected)) {
                throw new IllegalStateException("数据库未完成迁移");
            }
            return new SchemaCompatibility(applied);
        } catch (SQLException ex) {
            throw new IllegalStateException("数据库未完成迁移", ex);
        }
    }

    static Set<String> expectedVersions() {
        try {
            Resource[] resources = new PathMatchingResourcePatternResolver()
                    .getResources("classpath*:db/migration/V*.sql");
            Set<String> versions = new LinkedHashSet<>();
            for (Resource resource : resources) {
                String filename = resource.getFilename();
                if (filename == null || !filename.startsWith("V") || !filename.contains("__")) {
                    continue;
                }
                String version = filename.substring(1, filename.indexOf("__"));
                if (version.isBlank() || !versions.add(version)) {
                    throw new IllegalStateException("数据库未完成迁移");
                }
            }
            return versions;
        } catch (IOException ex) {
            throw new IllegalStateException("数据库未完成迁移", ex);
        }
    }
}
