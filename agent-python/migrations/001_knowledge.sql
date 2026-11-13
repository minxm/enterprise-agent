-- Knowledge documents and chunks. Embeddings stay out until the vector width is chosen.

CREATE TABLE agent.knowledge_document (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    document_name text NOT NULL,
    file_id uuid,
    file_type text NOT NULL,
    file_checksum text NOT NULL,
    status text NOT NULL,
    total_chunks integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT knowledge_document_id_tenant_unique UNIQUE (id, tenant_id),
    CONSTRAINT knowledge_document_name_check CHECK (char_length(document_name) BETWEEN 1 AND 200),
    CONSTRAINT knowledge_document_file_type_check CHECK (file_type IN ('pdf', 'docx')),
    CONSTRAINT knowledge_document_checksum_check CHECK (file_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT knowledge_document_status_check CHECK (status = 'ready'),
    CONSTRAINT knowledge_document_total_chunks_check CHECK (total_chunks >= 1)
);

CREATE UNIQUE INDEX knowledge_document_file_unique
    ON agent.knowledge_document (tenant_id, file_id)
    WHERE file_id IS NOT NULL;

CREATE UNIQUE INDEX knowledge_document_checksum_unique
    ON agent.knowledge_document (tenant_id, file_checksum)
    WHERE file_id IS NULL;

CREATE INDEX knowledge_document_tenant_idx
    ON agent.knowledge_document (tenant_id, created_at DESC);

CREATE TABLE agent.knowledge_chunk (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    document_id uuid NOT NULL,
    chunk_index integer NOT NULL,
    content text NOT NULL,
    char_count integer NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT knowledge_chunk_index_check CHECK (chunk_index >= 0),
    CONSTRAINT knowledge_chunk_content_check CHECK (char_length(btrim(content)) > 0),
    CONSTRAINT knowledge_chunk_char_count_check CHECK (char_count = char_length(content) AND char_count >= 1),
    CONSTRAINT knowledge_chunk_document_index_unique UNIQUE (document_id, chunk_index),
    CONSTRAINT knowledge_chunk_document_fk FOREIGN KEY (document_id, tenant_id)
        REFERENCES agent.knowledge_document (id, tenant_id)
        ON DELETE CASCADE
);

CREATE INDEX knowledge_chunk_tenant_document_idx
    ON agent.knowledge_chunk (tenant_id, document_id);

ALTER TABLE agent.knowledge_document ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.knowledge_document FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON agent.knowledge_document
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE agent.knowledge_chunk ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.knowledge_chunk FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON agent.knowledge_chunk
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

REVOKE ALL ON TABLE agent.knowledge_document FROM PUBLIC;
REVOKE ALL ON TABLE agent.knowledge_chunk FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE agent.knowledge_document TO agent_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE agent.knowledge_chunk TO agent_app;
