-- Generic file registry. Contract and policy tables stay for later business work.
-- Bytes stay in MinIO. This table stores metadata only.

CREATE TABLE business.stored_file (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES business.tenant (id),
    filename text NOT NULL,
    object_key text NOT NULL,
    bucket_name text NOT NULL,
    media_type text NOT NULL,
    byte_size bigint NOT NULL,
    extension text NOT NULL,
    checksum_sha256 text NOT NULL,
    uploaded_by uuid NOT NULL,
    status text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT stored_file_object_key_unique UNIQUE (object_key),
    CONSTRAINT stored_file_tenant_object_unique UNIQUE (tenant_id, object_key),
    CONSTRAINT stored_file_uploader_fk FOREIGN KEY (uploaded_by, tenant_id)
        REFERENCES business.app_user (id, tenant_id),
    CONSTRAINT stored_file_status_check CHECK (status IN ('ready', 'deleted', 'failed')),
    CONSTRAINT stored_file_extension_check CHECK (extension IN ('pdf', 'docx', 'doc', 'txt')),
    CONSTRAINT stored_file_byte_size_check CHECK (byte_size > 0),
    CONSTRAINT stored_file_checksum_check CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT stored_file_filename_check CHECK (length(filename) BETWEEN 1 AND 200),
    CONSTRAINT stored_file_bucket_check CHECK (length(bucket_name) BETWEEN 1 AND 63)
);

CREATE INDEX stored_file_tenant_idx ON business.stored_file (tenant_id, created_at DESC);

ALTER TABLE business.stored_file ENABLE ROW LEVEL SECURITY;
ALTER TABLE business.stored_file FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON business.stored_file
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
