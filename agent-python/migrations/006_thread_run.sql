-- Per-tenant fencing counter for one Knowledge QA thread.
--
-- The thread id string also contains the tenant, but the primary key does not
-- rely on that text. agent_migrator owns the table. Default privileges would
-- grant DELETE to agent_app, so this script revokes that and leaves runtime
-- DML explicit. The table stores no question, answer, token, or lock owner.
--
-- FORCE ROW LEVEL SECURITY also applies to the owner. The maintainer policy
-- lets agent_migrator remove rows. agent_app is not a member of that role.

CREATE TABLE agent.thread_run (
    tenant_id uuid NOT NULL,
    thread_id text NOT NULL,
    fence bigint NOT NULL,
    CONSTRAINT thread_run_pk PRIMARY KEY (tenant_id, thread_id),
    CONSTRAINT thread_run_fence_check CHECK (fence >= 1),
    CONSTRAINT thread_run_thread_id_check CHECK (
        thread_id ~ '^kq:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        AND thread_id = 'kq:' || tenant_id::text || ':' || split_part(thread_id, ':', 3)
    )
);

ALTER TABLE agent.thread_run ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent.thread_run FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON agent.thread_run
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY thread_run_maintainer ON agent.thread_run
    USING (current_user = 'agent_migrator')
    WITH CHECK (current_user = 'agent_migrator');

REVOKE ALL ON TABLE agent.thread_run FROM PUBLIC;
REVOKE ALL ON TABLE agent.thread_run FROM agent_app;
GRANT SELECT, INSERT, UPDATE ON TABLE agent.thread_run TO agent_app;
