package com.example.enterprise.config;

import org.flywaydb.core.Flyway;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.boot.SpringApplication;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;

@Component
@Profile("migrate")
public class BusinessSchemaMigration implements ApplicationRunner {

    private static final Logger log = LoggerFactory.getLogger(BusinessSchemaMigration.class);

    private final ConfigurableApplicationContext context;
    private final String url;
    private final String username;
    private final String password;
    private final boolean exitProcess;
    private int executed;

    public BusinessSchemaMigration(
            ConfigurableApplicationContext context,
            @Value("${spring.datasource.url}") String url,
            @Value("${spring.datasource.username}") String username,
            @Value("${spring.datasource.password}") String password,
            @Value("${enterprise.migration.exit:true}") boolean exitProcess) {
        this.context = context;
        this.url = url;
        this.username = username;
        this.password = password;
        this.exitProcess = exitProcess;
    }

    @Override
    public void run(ApplicationArguments args) {
        int code = 0;
        RuntimeException failure = null;
        try {
            executed = runOnce(url, username, password);
            if (executed == 0) {
                log.info("business schema 没有待执行的迁移");
            } else {
                log.info("business schema 迁移完成，新执行 {} 个版本", executed);
            }
        } catch (RuntimeException ex) {
            code = 1;
            failure = ex;
            log.error("数据库迁移失败: {}", safe(ex, password));
        }
        if (exitProcess) {
            int statusCode = code;
            int status = SpringApplication.exit(context, () -> statusCode);
            System.exit(status);
        }
        if (failure != null) {
            throw failure;
        }
    }

    public int executed() {
        return executed;
    }

    static int runOnce(String url, String username, String password) {
        Flyway flyway = BusinessFlyway.open(url, username, password);
        try {
            return BusinessFlyway.migrate(flyway);
        } finally {
            BusinessFlyway.close(flyway);
        }
    }

    private static String safe(RuntimeException ex, String password) {
        String text = ex.getMessage() == null ? ex.getClass().getSimpleName() : ex.getMessage();
        if (password != null && !password.isBlank()) {
            text = text.replace(password, "***");
        }
        String lower = text.toLowerCase();
        if (lower.contains("password=") || lower.contains("jdbc:")) {
            return ex.getClass().getSimpleName();
        }
        return text;
    }
}
