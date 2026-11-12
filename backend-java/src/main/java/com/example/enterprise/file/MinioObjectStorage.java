package com.example.enterprise.file;

import io.minio.BucketExistsArgs;
import io.minio.GetObjectArgs;
import io.minio.MakeBucketArgs;
import io.minio.MinioClient;
import io.minio.PutObjectArgs;
import io.minio.RemoveObjectArgs;
import io.minio.StatObjectArgs;
import io.minio.errors.ErrorResponseException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.ByteArrayInputStream;
import java.io.InputStream;

public class MinioObjectStorage implements ObjectStorage {

    private static final Logger log = LoggerFactory.getLogger(MinioObjectStorage.class);

    private final MinioClient client;
    private final String bucket;
    private volatile boolean bucketReady;

    public MinioObjectStorage(StorageProperties properties) {
        if (isBlank(properties.getEndpoint()) || isBlank(properties.getAccessKey()) || isBlank(properties.getSecretKey())
                || isBlank(properties.getBucket())) {
            throw new IllegalStateException("对象存储配置不完整");
        }
        this.bucket = properties.getBucket();
        this.client = MinioClient.builder()
                .endpoint(properties.getEndpoint())
                .credentials(properties.getAccessKey(), properties.getSecretKey())
                .build();
    }

    @Override
    public void upload(String bucketName, String objectKey, byte[] body, String mediaType) {
        ensureBucket();
        try {
            client.putObject(PutObjectArgs.builder()
                    .bucket(bucketName)
                    .object(objectKey)
                    .stream(new ByteArrayInputStream(body), body.length, -1)
                    .contentType(mediaType)
                    .build());
        } catch (Exception exception) {
            throw failure(exception);
        }
    }

    @Override
    public InputStream download(String bucketName, String objectKey) {
        ensureBucket();
        try {
            return client.getObject(GetObjectArgs.builder().bucket(bucketName).object(objectKey).build());
        } catch (Exception exception) {
            throw failure(exception);
        }
    }

    @Override
    public void delete(String bucketName, String objectKey) {
        ensureBucket();
        try {
            client.removeObject(RemoveObjectArgs.builder().bucket(bucketName).object(objectKey).build());
        } catch (ErrorResponseException exception) {
            if (isMissing(exception)) {
                return;
            }
            throw failure(exception);
        } catch (Exception exception) {
            throw failure(exception);
        }
    }

    @Override
    public boolean exists(String bucketName, String objectKey) {
        ensureBucket();
        try {
            client.statObject(StatObjectArgs.builder().bucket(bucketName).object(objectKey).build());
            return true;
        } catch (ErrorResponseException exception) {
            if (isMissing(exception)) {
                return false;
            }
            throw failure(exception);
        } catch (Exception exception) {
            throw failure(exception);
        }
    }

    private void ensureBucket() {
        if (bucketReady) {
            return;
        }
        synchronized (this) {
            if (bucketReady) {
                return;
            }
            try {
                boolean exists = client.bucketExists(BucketExistsArgs.builder().bucket(bucket).build());
                if (!exists) {
                    client.makeBucket(MakeBucketArgs.builder().bucket(bucket).build());
                }
                bucketReady = true;
            } catch (ErrorResponseException exception) {
                String code = exception.errorResponse().code();
                if ("BucketAlreadyOwnedByYou".equals(code) || "BucketAlreadyExists".equals(code)) {
                    bucketReady = true;
                    return;
                }
                throw failure(exception);
            } catch (Exception exception) {
                throw failure(exception);
            }
        }
    }

    private static boolean isMissing(ErrorResponseException exception) {
        String code = exception.errorResponse().code();
        return "NoSuchKey".equals(code) || "NoSuchObject".equals(code);
    }

    private static IllegalStateException failure(Exception exception) {
        String detail = exception instanceof ErrorResponseException error
                ? error.errorResponse().code()
                : exception.getClass().getSimpleName();
        log.warn("Object storage call failed: {}", detail);
        return new IllegalStateException("对象存储调用失败");
    }

    private static boolean isBlank(String value) {
        return value == null || value.isBlank();
    }
}
