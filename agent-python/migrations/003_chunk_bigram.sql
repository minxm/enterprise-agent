-- Character-pair index for keyword retrieval.
--
-- pg_trgm 1.6 is present in the server image but not installed. agent_migrator
-- is not a superuser and does not have CREATE on the database, so CREATE
-- EXTENSION pg_trgm is denied. zhparser and pg_jieba are not installed in the
-- image. These pairs are not Chinese word segmentation.
--
-- The migrator applies this script in one transaction, so CREATE INDEX
-- CONCURRENTLY cannot be used. Building the index blocks writes to
-- knowledge_chunk until the transaction finishes.

CREATE FUNCTION agent.character_bigrams(input text)
RETURNS text[]
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
AS $$
    SELECT COALESCE(
        array_agg(substring(input FROM index FOR 2) ORDER BY index),
        ARRAY[]::text[]
    )
    FROM generate_series(1, char_length(input) - 1) AS index
$$;

REVOKE ALL ON FUNCTION agent.character_bigrams(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent.character_bigrams(text) TO agent_app;

CREATE INDEX knowledge_chunk_content_bigram_idx
    ON agent.knowledge_chunk
    USING gin (agent.character_bigrams(content));
