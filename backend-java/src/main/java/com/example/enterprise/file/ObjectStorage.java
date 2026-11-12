package com.example.enterprise.file;

import java.io.InputStream;

public interface ObjectStorage {

    void upload(String bucket, String objectKey, byte[] body, String mediaType);

    InputStream download(String bucket, String objectKey);

    void delete(String bucket, String objectKey);

    boolean exists(String bucket, String objectKey);
}
