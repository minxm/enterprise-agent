-- LangGraph PostgresSaver tables for langgraph-checkpoint-postgres 3.2.0.
--
-- The library's PostgresSaver.setup() runs CREATE INDEX CONCURRENTLY, which
-- cannot run inside this migrator's single transaction. The statements below
-- match that version's MIGRATIONS list, with ordinary indexes instead.
-- checkpoint_migrations is seeded through version 9 so a later setup() call
-- does not repeat this DDL. The application does not call setup().
--
-- Tables live in the agent schema. agent_migrator owns them. agent_app receives
-- DML only and cannot create or alter these tables.

CREATE TABLE agent.checkpoint_migrations (
    v integer PRIMARY KEY
);

CREATE TABLE agent.checkpoints (
    thread_id text NOT NULL,
    checkpoint_ns text NOT NULL DEFAULT '',
    checkpoint_id text NOT NULL,
    parent_checkpoint_id text,
    type text,
    checkpoint jsonb NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

CREATE TABLE agent.checkpoint_blobs (
    thread_id text NOT NULL,
    checkpoint_ns text NOT NULL DEFAULT '',
    channel text NOT NULL,
    version text NOT NULL,
    type text NOT NULL,
    blob bytea,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);

CREATE TABLE agent.checkpoint_writes (
    thread_id text NOT NULL,
    checkpoint_ns text NOT NULL DEFAULT '',
    checkpoint_id text NOT NULL,
    task_id text NOT NULL,
    idx integer NOT NULL,
    channel text NOT NULL,
    type text,
    blob bytea NOT NULL,
    task_path text NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

CREATE INDEX checkpoints_thread_id_idx ON agent.checkpoints (thread_id);
CREATE INDEX checkpoint_blobs_thread_id_idx ON agent.checkpoint_blobs (thread_id);
CREATE INDEX checkpoint_writes_thread_id_idx ON agent.checkpoint_writes (thread_id);

INSERT INTO agent.checkpoint_migrations (v) VALUES
    (0), (1), (2), (3), (4), (5), (6), (7), (8), (9);

REVOKE ALL ON TABLE agent.checkpoint_migrations FROM PUBLIC;
REVOKE ALL ON TABLE agent.checkpoints FROM PUBLIC;
REVOKE ALL ON TABLE agent.checkpoint_blobs FROM PUBLIC;
REVOKE ALL ON TABLE agent.checkpoint_writes FROM PUBLIC;
REVOKE ALL ON TABLE agent.checkpoint_migrations FROM agent_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE agent.checkpoints TO agent_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE agent.checkpoint_blobs TO agent_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE agent.checkpoint_writes TO agent_app;
