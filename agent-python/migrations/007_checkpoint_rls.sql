-- Tenant isolation for LangGraph checkpoint tables.
--
-- The tables keep the column list from langgraph-checkpoint-postgres 3.2.0.
-- Isolation compares the transaction-local app.tenant_id with the tenant
-- segment of thread_id. It does not read metadata JSON and does not require an
-- agent.thread_run row. agent_migrator retains a separate maintainer policy.
-- agent_app is not the owner and does not receive BYPASSRLS.
--
-- Rollback belongs in the delivery report, not in this file. Running it as
-- agent_migrator drops only checkpoint_tenant and checkpoint_maintainer on
-- these three tables and disables row level security. It does not delete rows.

ALTER TABLE agent.checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.checkpoints FORCE ROW LEVEL SECURITY;
ALTER TABLE agent.checkpoint_blobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.checkpoint_blobs FORCE ROW LEVEL SECURITY;
ALTER TABLE agent.checkpoint_writes ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.checkpoint_writes FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS checkpoint_tenant ON agent.checkpoints;
DROP POLICY IF EXISTS checkpoint_maintainer ON agent.checkpoints;
DROP POLICY IF EXISTS checkpoint_tenant ON agent.checkpoint_blobs;
DROP POLICY IF EXISTS checkpoint_maintainer ON agent.checkpoint_blobs;
DROP POLICY IF EXISTS checkpoint_tenant ON agent.checkpoint_writes;
DROP POLICY IF EXISTS checkpoint_maintainer ON agent.checkpoint_writes;

CREATE POLICY checkpoint_tenant ON agent.checkpoints
    USING (
        thread_id ~ '^kq:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        AND current_setting('app.tenant_id', true) ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        AND split_part(thread_id, ':', 2) = current_setting('app.tenant_id', true)
        AND thread_id = 'kq:' || current_setting('app.tenant_id', true) || ':' || split_part(thread_id, ':', 3)
    )
    WITH CHECK (
        thread_id ~ '^kq:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        AND current_setting('app.tenant_id', true) ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        AND split_part(thread_id, ':', 2) = current_setting('app.tenant_id', true)
        AND thread_id = 'kq:' || current_setting('app.tenant_id', true) || ':' || split_part(thread_id, ':', 3)
    );

CREATE POLICY checkpoint_maintainer ON agent.checkpoints
    USING (current_user = 'agent_migrator')
    WITH CHECK (current_user = 'agent_migrator');

CREATE POLICY checkpoint_tenant ON agent.checkpoint_blobs
    USING (
        thread_id ~ '^kq:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        AND current_setting('app.tenant_id', true) ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        AND split_part(thread_id, ':', 2) = current_setting('app.tenant_id', true)
        AND thread_id = 'kq:' || current_setting('app.tenant_id', true) || ':' || split_part(thread_id, ':', 3)
    )
    WITH CHECK (
        thread_id ~ '^kq:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        AND current_setting('app.tenant_id', true) ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        AND split_part(thread_id, ':', 2) = current_setting('app.tenant_id', true)
        AND thread_id = 'kq:' || current_setting('app.tenant_id', true) || ':' || split_part(thread_id, ':', 3)
    );

CREATE POLICY checkpoint_maintainer ON agent.checkpoint_blobs
    USING (current_user = 'agent_migrator')
    WITH CHECK (current_user = 'agent_migrator');

CREATE POLICY checkpoint_tenant ON agent.checkpoint_writes
    USING (
        thread_id ~ '^kq:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        AND current_setting('app.tenant_id', true) ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        AND split_part(thread_id, ':', 2) = current_setting('app.tenant_id', true)
        AND thread_id = 'kq:' || current_setting('app.tenant_id', true) || ':' || split_part(thread_id, ':', 3)
    )
    WITH CHECK (
        thread_id ~ '^kq:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        AND current_setting('app.tenant_id', true) ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        AND split_part(thread_id, ':', 2) = current_setting('app.tenant_id', true)
        AND thread_id = 'kq:' || current_setting('app.tenant_id', true) || ':' || split_part(thread_id, ':', 3)
    );

CREATE POLICY checkpoint_maintainer ON agent.checkpoint_writes
    USING (current_user = 'agent_migrator')
    WITH CHECK (current_user = 'agent_migrator');
