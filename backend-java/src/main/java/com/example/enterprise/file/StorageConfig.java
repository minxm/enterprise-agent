package com.example.enterprise.file;

import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Lazy;
import org.springframework.context.annotation.Profile;

@Configuration
@Profile("dev")
@EnableConfigurationProperties(StorageProperties.class)
public class StorageConfig {

    @Bean
    @Lazy
    public ObjectStorage objectStorage(StorageProperties properties) {
        return new MinioObjectStorage(properties);
    }
}
