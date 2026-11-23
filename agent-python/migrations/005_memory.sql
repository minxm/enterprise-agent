-- Long-term memory. No embeddings in this version.
--
-- agent_migrator owns the tables. Default privileges would grant DELETE to
-- agent_app, so this script revokes that and leaves runtime DML explicit.
-- GUC values are not credentials. RLS only reads the context the application
-- already validated and set for this transaction.
--
-- FORCE ROW LEVEL SECURITY also applies to the owner. The maintainer
-- policies below let agent_migrator purge rows. agent_app is not a member
-- of that role and cannot SET ROLE to it.

CREATE TABLE agent.memory_item (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    user_id uuid,
    scope text NOT NULL,
    memory_type text NOT NULL,
    content text NOT NULL,
    source text NOT NULL,
    source_conversation_id uuid,
    confidence numeric(3, 2) NOT NULL,
    status text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    deleted_at timestamptz,
    created_by uuid NOT NULL,
    updated_by uuid,
    version integer NOT NULL DEFAULT 1,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    content_sha256 text NOT NULL,
    CONSTRAINT memory_item_scope_check CHECK (scope IN ('user', 'tenant')),
    CONSTRAINT memory_item_scope_owner_check CHECK (
        (scope = 'user' AND user_id IS NOT NULL)
        OR (scope = 'tenant' AND user_id IS NULL)
    ),
    CONSTRAINT memory_item_type_check CHECK (
        memory_type IN ('preference', 'format', 'work_scope', 'organization_practice')
    ),
    CONSTRAINT memory_item_source_check CHECK (source = 'user_explicit'),
    CONSTRAINT memory_item_confidence_check CHECK (confidence = 1.00),
    CONSTRAINT memory_item_status_check CHECK (
        status IN ('active', 'archived', 'expired', 'deleted')
    ),
    CONSTRAINT memory_item_version_check CHECK (version >= 1),
    CONSTRAINT memory_item_sha_check CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT memory_item_metadata_object_check CHECK (jsonb_typeof(metadata) = 'object'),
    CONSTRAINT memory_item_lifecycle_check CHECK (
        (
            status = 'deleted'
            AND content = ''
            AND metadata = '{}'::jsonb
            AND deleted_at IS NOT NULL
        )
        OR (
            status <> 'deleted'
            AND content = btrim(content)
            AND char_length(content) BETWEEN 1 AND 1000
            AND deleted_at IS NULL
        )
    )
);

CREATE TABLE agent.memory_audit (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    action text NOT NULL,
    request_id text,
    content_sha256 text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT memory_audit_action_check CHECK (
        action IN ('create', 'update', 'archive', 'expire', 'delete')
    ),
    CONSTRAINT memory_audit_sha_check CHECK (
        content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT memory_audit_request_check CHECK (
        request_id IS NULL OR char_length(request_id) BETWEEN 1 AND 128
    )
);

CREATE INDEX memory_item_user_idx
    ON agent.memory_item (tenant_id, user_id, updated_at DESC)
    WHERE scope = 'user' AND status <> 'deleted';

CREATE INDEX memory_item_tenant_idx
    ON agent.memory_item (tenant_id, updated_at DESC)
    WHERE scope = 'tenant' AND status <> 'deleted';

CREATE INDEX memory_item_expires_idx
    ON agent.memory_item (expires_at)
    WHERE status = 'active' AND expires_at IS NOT NULL;

CREATE INDEX memory_audit_tenant_time_idx
    ON agent.memory_audit (tenant_id, created_at DESC);

ALTER TABLE agent.memory_item ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.memory_item FORCE ROW LEVEL SECURITY;

CREATE POLICY memory_item_user ON agent.memory_item
    USING (
        tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        AND scope = 'user'
        AND user_id = NULLIF(current_setting('app.actor_id', true), '')::uuid
    )
    WITH CHECK (
        tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        AND scope = 'user'
        AND user_id = NULLIF(current_setting('app.actor_id', true), '')::uuid
    );

CREATE POLICY memory_item_tenant_read ON agent.memory_item
    FOR SELECT
    USING (
        tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        AND scope = 'tenant'
        AND (
            current_setting('app.tenant_memory_read', true) = 'on'
            OR current_setting('app.tenant_memory_write', true) = 'on'
        )
    );

CREATE POLICY memory_item_tenant_insert ON agent.memory_item
    FOR INSERT
    WITH CHECK (
        tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        AND scope = 'tenant'
        AND current_setting('app.tenant_memory_write', true) = 'on'
    );

CREATE POLICY memory_item_tenant_update ON agent.memory_item
    FOR UPDATE
    USING (
        tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        AND scope = 'tenant'
        AND current_setting('app.tenant_memory_write', true) = 'on'
    )
    WITH CHECK (
        tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        AND scope = 'tenant'
        AND current_setting('app.tenant_memory_write', true) = 'on'
    );

CREATE POLICY memory_item_maintainer ON agent.memory_item
    USING (current_user = 'agent_migrator')
    WITH CHECK (current_user = 'agent_migrator');

ALTER TABLE agent.memory_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.memory_audit FORCE ROW LEVEL SECURITY;

CREATE POLICY memory_audit_tenant ON agent.memory_audit
    USING (
        tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        AND (
            actor_id = NULLIF(current_setting('app.actor_id', true), '')::uuid
            OR current_setting('app.tenant_memory_read', true) = 'on'
            OR current_setting('app.tenant_memory_write', true) = 'on'
        )
    )
    WITH CHECK (
        tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        AND actor_id = NULLIF(current_setting('app.actor_id', true), '')::uuid
    );

CREATE POLICY memory_audit_maintainer ON agent.memory_audit
    USING (current_user = 'agent_migrator')
    WITH CHECK (current_user = 'agent_migrator');

CREATE FUNCTION agent.memory_audit_immutable()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
AS $memory_audit$
BEGIN
    IF current_user <> 'agent_migrator' THEN
        RAISE EXCEPTION 'memory_audit is append-only'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$memory_audit$;

CREATE TRIGGER memory_audit_no_change
    BEFORE UPDATE OR DELETE ON agent.memory_audit
    FOR EACH ROW
    EXECUTE FUNCTION agent.memory_audit_immutable();

REVOKE ALL ON FUNCTION agent.memory_audit_immutable() FROM PUBLIC;

REVOKE ALL ON TABLE agent.memory_item FROM PUBLIC;
REVOKE ALL ON TABLE agent.memory_item FROM agent_app;
GRANT SELECT, INSERT, UPDATE ON TABLE agent.memory_item TO agent_app;

REVOKE ALL ON TABLE agent.memory_audit FROM PUBLIC;
REVOKE ALL ON TABLE agent.memory_audit FROM agent_app;
GRANT SELECT, INSERT ON TABLE agent.memory_audit TO agent_app;
