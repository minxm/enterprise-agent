package com.example.enterprise.config;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import io.lettuce.core.ClientOptions;
import io.lettuce.core.SocketOptions;
import java.time.Duration;
import org.flywaydb.core.Flyway;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Profile;
import org.springframework.data.redis.connection.RedisStandaloneConfiguration;
import org.springframework.data.redis.connection.lettuce.LettuceClientConfiguration;
import org.springframework.data.redis.connection.lettuce.LettuceConnectionFactory;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.transaction.PlatformTransactionManager;

import javax.sql.DataSource;

@Configuration
@Profile({"dev", "prod"})
public class DevInfrastructureConfig {

    private static final Logger log = LoggerFactory.getLogger(DevInfrastructureConfig.class);

    @Bean
    public DataSource dataSource(
            @Value("${spring.datasource.url}") String url,
            @Value("${spring.datasource.username}") String username,
            @Value("${spring.datasource.password}") String password,
            @Value("${spring.datasource.hikari.pool-name:business-app}") String poolName,
            @Value("${spring.datasource.hikari.maximum-pool-size:10}") int maximumPoolSize,
            @Value("${spring.datasource.hikari.minimum-idle:1}") int minimumIdle,
            @Value("${spring.datasource.hikari.connection-timeout:3000}") long connectionTimeout) {
        require("spring.datasource.url", url);
        require("spring.datasource.password", password);
        if (!"business_app".equals(username)) {
            throw new IllegalStateException("业务连接必须使用 business_app");
        }
        HikariConfig config = new HikariConfig();
        config.setJdbcUrl(url);
        config.setUsername(username);
        config.setPassword(password);
        config.setPoolName(poolName);
        config.setMaximumPoolSize(maximumPoolSize);
        config.setMinimumIdle(minimumIdle);
        config.setConnectionTimeout(connectionTimeout);
        config.setConnectionInitSql("SET search_path TO business");
        return new HikariDataSource(config);
    }

    @Bean
    public JdbcTemplate jdbcTemplate(DataSource dataSource) {
        return new JdbcTemplate(dataSource);
    }

    @Bean
    public PlatformTransactionManager transactionManager(DataSource dataSource) {
        return new DataSourceTransactionManager(dataSource);
    }

    @Bean
    @Profile("dev")
    public Flyway flyway(
            @Value("${spring.datasource.url}") String url,
            @Value("${spring.flyway.user}") String username,
            @Value("${spring.flyway.password}") String password) {
        Flyway flyway = BusinessFlyway.open(url, username, password);
        BusinessFlyway.migrate(flyway);
        return flyway;
    }

    @Bean
    @Profile("prod")
    public SchemaCompatibility schemaCompatibility(DataSource dataSource) {
        SchemaCompatibility status = BusinessSchemaGuard.verify(dataSource);
        log.info("business schema 兼容，未执行迁移");
        return status;
    }

    @Bean
    public LettuceConnectionFactory redisConnectionFactory(
            @Value("${spring.data.redis.host}") String host,
            @Value("${spring.data.redis.port}") int port,
            @Value("${spring.data.redis.password}") String password,
            @Value("${spring.data.redis.timeout:2s}") Duration timeout) {
        require("spring.data.redis.host", host);
        require("spring.data.redis.password", password);
        if (timeout == null || timeout.isZero() || timeout.isNegative()) {
            throw new IllegalStateException("基础设施配置不完整: spring.data.redis.timeout");
        }
        RedisStandaloneConfiguration configuration = new RedisStandaloneConfiguration(host, port);
        configuration.setPassword(password);
        LettuceClientConfiguration client = LettuceClientConfiguration.builder()
                .commandTimeout(timeout)
                .clientOptions(ClientOptions.builder()
                        .socketOptions(SocketOptions.builder().connectTimeout(timeout).build())
                        .build())
                .build();
        return new LettuceConnectionFactory(configuration, client);
    }

    @Bean
    public StringRedisTemplate stringRedisTemplate(LettuceConnectionFactory connectionFactory) {
        return new StringRedisTemplate(connectionFactory);
    }

    private static void require(String name, String value) {
        if (value == null || value.isBlank()) {
            throw new IllegalStateException("基础设施配置不完整: " + name);
        }
    }
}
