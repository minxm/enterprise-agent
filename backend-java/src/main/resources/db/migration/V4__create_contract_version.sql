-- Contract versions reference stored_file. They do not copy bytes or object keys,
-- and they do not replace the contract_file attachment association.

CREATE TABLE business.contract_version (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    contract_id uuid NOT NULL,
    version_no integer NOT NULL,
    version_label text NOT NULL,
    file_id uuid NOT NULL,
    change_summary text NOT NULL,
    is_current boolean NOT NULL,
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT contract_version_id_tenant_unique UNIQUE (id, tenant_id),
    CONSTRAINT contract_version_no_unique UNIQUE (contract_id, version_no),
    CONSTRAINT contract_version_contract_fk FOREIGN KEY (contract_id, tenant_id)
        REFERENCES business.contract (id, tenant_id),
    CONSTRAINT contract_version_file_fk FOREIGN KEY (file_id, tenant_id)
        REFERENCES business.stored_file (id, tenant_id),
    CONSTRAINT contract_version_creator_fk FOREIGN KEY (created_by, tenant_id)
        REFERENCES business.app_user (id, tenant_id),
    CONSTRAINT contract_version_no_check CHECK (version_no >= 1),
    CONSTRAINT contract_version_label_check CHECK (version_label = 'V' || version_no::text),
    CONSTRAINT contract_version_summary_check CHECK (length(change_summary) BETWEEN 1 AND 500)
);

CREATE UNIQUE INDEX contract_version_current_unique
    ON business.contract_version (contract_id)
    WHERE is_current;

CREATE INDEX contract_version_tenant_idx
    ON business.contract_version (tenant_id, contract_id, version_no DESC);

ALTER TABLE business.contract_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE business.contract_version FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON business.contract_version
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
