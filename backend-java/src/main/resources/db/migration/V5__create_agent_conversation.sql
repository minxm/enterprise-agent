-- Minimum conversation ownership for the Knowledge QA gateway.
-- The agent checkpointer stays in the agent schema. This table only records
-- which business user may open a conversation.

CREATE TABLE business.agent_conversation (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES business.tenant (id),
    owner_user_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT agent_conversation_owner_fk FOREIGN KEY (owner_user_id, tenant_id)
        REFERENCES business.app_user (id, tenant_id)
);

CREATE INDEX agent_conversation_owner_idx
    ON business.agent_conversation (tenant_id, owner_user_id);

ALTER TABLE business.agent_conversation ENABLE ROW LEVEL SECURITY;
ALTER TABLE business.agent_conversation FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON business.agent_conversation
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

REVOKE ALL ON TABLE business.agent_conversation FROM PUBLIC;
GRANT SELECT, INSERT, DELETE ON TABLE business.agent_conversation TO business_app;
