-- Identity tables follow the approved business schema names.
-- sys_* names are not used. No agent schema objects are created here.

CREATE TABLE business.tenant (
    id uuid PRIMARY KEY,
    code text NOT NULL,
    name text NOT NULL,
    status text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tenant_code_unique UNIQUE (code),
    CONSTRAINT tenant_status_check CHECK (status IN ('active', 'disabled'))
);

CREATE TABLE business.app_user (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES business.tenant (id),
    login_name text NOT NULL,
    password_hash text NOT NULL,
    display_name text NOT NULL,
    status text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT app_user_login_unique UNIQUE (tenant_id, login_name),
    CONSTRAINT app_user_id_tenant_unique UNIQUE (id, tenant_id),
    CONSTRAINT app_user_status_check CHECK (status IN ('active', 'disabled'))
);

CREATE INDEX app_user_tenant_idx ON business.app_user (tenant_id);

CREATE TABLE business.role (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES business.tenant (id),
    code text NOT NULL,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT role_code_unique UNIQUE (tenant_id, code),
    CONSTRAINT role_id_tenant_unique UNIQUE (id, tenant_id),
    CONSTRAINT role_code_check CHECK (code IN ('tenant_admin', 'contract_manager', 'approver'))
);

CREATE INDEX role_tenant_idx ON business.role (tenant_id);

CREATE TABLE business.permission (
    id uuid PRIMARY KEY,
    code text NOT NULL,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT permission_code_unique UNIQUE (code)
);

CREATE TABLE business.user_role (
    user_id uuid NOT NULL,
    role_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, role_id),
    CONSTRAINT user_role_user_fk FOREIGN KEY (user_id, tenant_id)
        REFERENCES business.app_user (id, tenant_id),
    CONSTRAINT user_role_role_fk FOREIGN KEY (role_id, tenant_id)
        REFERENCES business.role (id, tenant_id)
);

CREATE INDEX user_role_tenant_idx ON business.user_role (tenant_id);

CREATE TABLE business.role_permission (
    role_id uuid NOT NULL,
    permission_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (role_id, permission_id),
    CONSTRAINT role_permission_role_fk FOREIGN KEY (role_id, tenant_id)
        REFERENCES business.role (id, tenant_id),
    CONSTRAINT role_permission_permission_fk FOREIGN KEY (permission_id)
        REFERENCES business.permission (id)
);

CREATE INDEX role_permission_tenant_idx ON business.role_permission (tenant_id);

ALTER TABLE business.app_user ENABLE ROW LEVEL SECURITY;
ALTER TABLE business.app_user FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON business.app_user
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE business.role ENABLE ROW LEVEL SECURITY;
ALTER TABLE business.role FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON business.role
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE business.user_role ENABLE ROW LEVEL SECURITY;
ALTER TABLE business.user_role FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON business.user_role
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

ALTER TABLE business.role_permission ENABLE ROW LEVEL SECURITY;
ALTER TABLE business.role_permission FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON business.role_permission
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

INSERT INTO business.permission (id, code, name)
VALUES
    (gen_random_uuid(), 'tenant:manage', '用户与角色'),
    (gen_random_uuid(), 'contract:read', '读合同与会话'),
    (gen_random_uuid(), 'contract:write', '建合同、上传、撤回'),
    (gen_random_uuid(), 'contract:submit', '提交审核并允许保存草稿'),
    (gen_random_uuid(), 'approval:decide', '通过或驳回'),
    (gen_random_uuid(), 'policy:manage', '条款库');
