-- Fixed 1024-dimension embeddings. Vector indexes stay out until retrieval exists.

ALTER TABLE agent.knowledge_chunk
    ADD COLUMN embedding public.vector(1024),
    ADD COLUMN embedding_model text,
    ADD COLUMN embedded_at timestamptz;

ALTER TABLE agent.knowledge_chunk
    ADD CONSTRAINT knowledge_chunk_embedding_state_check
    CHECK (
        (
            embedding IS NULL
            AND embedding_model IS NULL
            AND embedded_at IS NULL
        )
        OR
        (
            embedding IS NOT NULL
            AND embedding_model IS NOT NULL
            AND char_length(embedding_model) BETWEEN 1 AND 200
            AND embedded_at IS NOT NULL
        )
    );
