-- Business contract registry. Agent review states are not stored here.
-- contract_file points at stored_file and does not copy object bytes or keys.

ALTER TABLE business.stored_file
    ADD CONSTRAINT stored_file_id_tenant_unique UNIQUE (id, tenant_id);

CREATE TABLE business.contract (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES business.tenant (id),
    contract_no text NOT NULL,
    title text NOT NULL,
    contract_type text NOT NULL,
    counterparty_name text NOT NULL,
    amount numeric(18, 2) NOT NULL,
    currency text NOT NULL,
    sign_date date,
    effective_date date,
    expiry_date date,
    status text NOT NULL,
    description text,
    created_by uuid NOT NULL,
    updated_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT contract_no_unique UNIQUE (tenant_id, contract_no),
    CONSTRAINT contract_id_tenant_unique UNIQUE (id, tenant_id),
    CONSTRAINT contract_creator_fk FOREIGN KEY (created_by, tenant_id)
        REFERENCES business.app_user (id, tenant_id),
    CONSTRAINT contract_updater_fk FOREIGN KEY (updated_by, tenant_id)
        REFERENCES business.app_user (id, tenant_id),
    CONSTRAINT contract_status_check CHECK (status IN (
        'draft', 'pending_review', 'approved', 'rejected', 'effective', 'expired', 'terminated')),
    CONSTRAINT contract_amount_check CHECK (amount >= 0),
    CONSTRAINT contract_currency_check CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT contract_date_check CHECK (
        effective_date IS NULL OR expiry_date IS NULL OR effective_date <= expiry_date),
    CONSTRAINT contract_no_check CHECK (length(contract_no) BETWEEN 1 AND 64),
    CONSTRAINT contract_title_check CHECK (length(title) BETWEEN 1 AND 200),
    CONSTRAINT contract_type_check CHECK (length(contract_type) BETWEEN 1 AND 50),
    CONSTRAINT contract_counterparty_check CHECK (length(counterparty_name) BETWEEN 1 AND 200),
    CONSTRAINT contract_description_check CHECK (description IS NULL OR length(description) <= 2000)
);

CREATE INDEX contract_tenant_idx ON business.contract (tenant_id, created_at DESC);
CREATE INDEX contract_tenant_status_idx ON business.contract (tenant_id, status);

ALTER TABLE business.contract ENABLE ROW LEVEL SECURITY;
ALTER TABLE business.contract FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON business.contract
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE TABLE business.contract_file (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    contract_id uuid NOT NULL,
    file_id uuid NOT NULL,
    file_role text NOT NULL,
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT contract_file_link_unique UNIQUE (contract_id, file_id),
    CONSTRAINT contract_file_role_check CHECK (file_role IN ('original', 'attachment', 'supplement', 'other')),
    CONSTRAINT contract_file_contract_fk FOREIGN KEY (contract_id, tenant_id)
        REFERENCES business.contract (id, tenant_id),
    CONSTRAINT contract_file_file_fk FOREIGN KEY (file_id, tenant_id)
        REFERENCES business.stored_file (id, tenant_id),
    CONSTRAINT contract_file_creator_fk FOREIGN KEY (created_by, tenant_id)
        REFERENCES business.app_user (id, tenant_id)
);

CREATE INDEX contract_file_tenant_idx ON business.contract_file (tenant_id, contract_id);

ALTER TABLE business.contract_file ENABLE ROW LEVEL SECURITY;
ALTER TABLE business.contract_file FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON business.contract_file
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
