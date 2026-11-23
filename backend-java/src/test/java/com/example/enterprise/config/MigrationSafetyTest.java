package com.example.enterprise.config;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.example.enterprise.EnterpriseApplication;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.Test;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.core.io.ClassPathResource;
import org.springframework.core.io.support.PathMatchingResourcePatternResolver;
import org.springframework.util.StreamUtils;

class MigrationSafetyTest {

    @Test
    void prodConfigDoesNotRequireTheMigratorPassword() throws Exception {
        String prod = StreamUtils.copyToString(
                new ClassPathResource("application-prod.yml").getInputStream(), StandardCharsets.UTF_8);
        String migrate = StreamUtils.copyToString(
                new ClassPathResource("application-migrate.yml").getInputStream(), StandardCharsets.UTF_8);
        assertThat(prod).doesNotContain("BUSINESS_MIGRATOR_PASSWORD");
        assertThat(prod).contains("username: business_app");
        assertThat(migrate).contains("username: business_migrator");
        assertThat(migrate).contains("BUSINESS_MIGRATOR_PASSWORD");
        assertThat(migrate).doesNotContain("business_app");
    }

    @Test
    void blankMigratorPasswordFailsBeforeConnecting() {
        assertThatThrownBy(() -> BusinessSchemaMigration.runOnce(
                "jdbc:postgresql://127.0.0.1:1/enterprise", "business_migrator", " "))
                .isInstanceOf(IllegalStateException.class)
                .hasMessage("迁移配置不完整: BUSINESS_MIGRATOR_PASSWORD");
    }

    @Test
    void businessAppIsRejectedBeforeConnecting() {
        assertThatThrownBy(() -> BusinessSchemaMigration.runOnce(
                "jdbc:postgresql://127.0.0.1:1/enterprise", "business_app", "not-used"))
                .isInstanceOf(IllegalStateException.class)
                .hasMessage("Flyway 必须使用 business_migrator");
    }

    @Test
    void failedConnectionDoesNotEchoThePassword() {
        String secret = "day47-secret-value";
        assertThatThrownBy(() -> BusinessSchemaMigration.runOnce(
                "jdbc:postgresql://127.0.0.1:1/enterprise", "business_migrator", secret))
                .isInstanceOf(IllegalStateException.class)
                .satisfies(error -> assertThat(messages(error)).doesNotContain(secret).doesNotContain("password="));
    }

    @Test
    void migrateProfileWithoutPasswordDoesNotStart() {
        assertThatThrownBy(() -> new SpringApplicationBuilder(EnterpriseApplication.class)
                .profiles("migrate")
                .run(
                        "--spring.datasource.url=jdbc:postgresql://127.0.0.1:1/enterprise",
                        "--spring.datasource.username=business_migrator",
                        "--spring.datasource.password=",
                        "--enterprise.migration.exit=false"))
                .satisfies(error -> assertThat(messages(error)).contains("BUSINESS_MIGRATOR_PASSWORD"));
    }

    @Test
    void flywayHistoryPrivilegeIsOutsideVersionedMigrations() throws Exception {
        Path script = Path.of("database", "postgres", "privileges", "business_flyway_history.sql");
        if (!Files.exists(script)) {
            script = Path.of("..", "database", "postgres", "privileges", "business_flyway_history.sql");
        }
        String privilegeSql = Files.readString(script).toLowerCase();
        assertThat(privilegeSql).contains("business_migrator");
        for (String statement : BusinessFlyway.HISTORY_PRIVILEGE_STATEMENTS) {
            assertThat(privilegeSql).contains(statement);
        }
        var resources = new PathMatchingResourcePatternResolver().getResources("classpath*:db/migration/V*.sql");
        assertThat(resources).isNotEmpty();
        for (var resource : resources) {
            String sql = StreamUtils.copyToString(resource.getInputStream(), StandardCharsets.UTF_8).toLowerCase();
            assertThat(sql).doesNotContain("flyway_schema_history");
        }
    }

    @Test
    void prodWithoutAppPasswordDoesNotStart() {
        assertThatThrownBy(() -> new SpringApplicationBuilder(EnterpriseApplication.class)
                .profiles("prod")
                .properties(
                        "MINIO_ENDPOINT=http://127.0.0.1:9000",
                        "MINIO_ACCESS_KEY=prod-profile-test-access",
                        "MINIO_SECRET_KEY=prod-profile-test-secret",
                        "AGENT_SERVICE_BASE_URL=http://127.0.0.1:8000",
                        "spring.data.redis.host=127.0.0.1",
                        "spring.data.redis.password=unused",
                        "enterprise.auth.secret=0123456789abcdef0123456789abcdef")
                .run(
                        "--spring.datasource.url=jdbc:postgresql://127.0.0.1:1/enterprise",
                        "--spring.datasource.password=",
                        "--spring.main.web-application-type=none"))
                .satisfies(error -> assertThat(messages(error)).contains("spring.datasource.password"));
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
