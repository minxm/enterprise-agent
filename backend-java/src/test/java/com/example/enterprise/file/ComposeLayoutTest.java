package com.example.enterprise.file;

import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class ComposeLayoutTest {

    @Test
    void composePinsMinioWithoutReplacingDatabaseServices() throws Exception {
        String compose = Files.readString(Path.of("..", "deploy", "docker-compose.yml"));

        assertTrue(compose.contains("image: pgvector/pgvector:0.8.7-pg16"));
        assertTrue(compose.contains("image: redis:7.4.12-alpine"));
        assertTrue(compose.contains("ghcr.io/tsouza/cerberus-mirror/minio/minio:RELEASE.2025-09-07T16-13-09Z"));
        assertTrue(compose.contains("ghcr.io/tsouza/cerberus-mirror/minio/mc:RELEASE.2025-08-13T08-35-41Z"));
        assertTrue(compose.contains("127.0.0.1:${MINIO_API_PORT:-9000}:9000"));
        assertTrue(compose.contains("127.0.0.1:${MINIO_CONSOLE_PORT:-9001}:9001"));
        assertTrue(compose.contains("minio-data:/data"));
        assertTrue(compose.contains("\"mc\", \"ready\", \"local\""));
        assertTrue(compose.contains("mc mb --ignore-existing"));
        assertFalse(compose.contains(":latest"));
        assertTrue(compose.contains("postgres-data:"));
        assertTrue(compose.contains("redis-data:"));
    }
}
