import hashlib
import json
import threading
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg import Connection, OperationalError
from psycopg.errors import DivisionByZero, ForeignKeyViolation, InsufficientPrivilege
from psycopg_pool import ConnectionPool
from pypdf import PdfWriter

from app.core.config import Settings
from app.documents.extract import (
    DocumentValidationError,
    NoExtractableTextError,
    extract_document,
)
from app.knowledge.chunking import chunk_text
from app.knowledge.ingest import IngestDocument, KnowledgeIngestionService
from app.persistence.migrate import _split_sql, apply_migrations
from app.persistence.postgres import build_conninfo, tenant_transaction
from test_extract import _docx, _pdf_pages


def test_migration_requires_a_password():
    settings = Settings(
        postgres_host="127.0.0.1",
        postgres_db="enterprise",
        agent_app_password="unused",
        agent_migrator_password="",
    )

    with pytest.raises(RuntimeError, match="AGENT_MIGRATOR_PASSWORD"):
        apply_migrations(settings)


def test_sql_splitter_keeps_text_inside_quotes():
    statements = _split_sql("select ';';\nselect 1;")

    assert statements == ["select ';'", "select 1"]


def test_knowledge_migration_does_not_touch_business_or_vectors():
    script = Path(__file__).resolve().parents[1].joinpath("migrations", "001_knowledge.sql")
    sql = script.read_text(encoding="utf-8").lower()

    assert "business." not in sql
    assert "vector(" not in sql
    assert "token_count" not in sql


def test_pdf_document_is_stored(knowledge_db):
    tenant_id = uuid4()
    content = _pdf_pages(["Contract alpha"])
    try:
        result = _service(knowledge_db).ingest(
            _request(tenant_id, content, "pdf", "alpha.pdf", uuid4())
        )
        stored = _document(knowledge_db.pool, tenant_id, result.document_id)
        chunks = _chunks(knowledge_db.pool, tenant_id, result.document_id)

        assert result.status == "ready"
        assert result.reused is False
        assert result.chunk_count == len(chunks) == stored[4]
        assert stored[1] == "pdf"
        assert stored[2] == hashlib.sha256(content).hexdigest()
        assert stored[3] == "ready"
        assert chunks[0][1] == "Contract alpha"
        assert chunks[0][0] == 0
        assert _metadata(chunks[0][3])["file_type"] == "pdf"
        _assert_tenant_does_not_leak(knowledge_db.pool)
    finally:
        _delete_tenant(knowledge_db.pool, tenant_id)


def test_docx_document_is_stored_with_ordered_chunks(knowledge_db):
    tenant_id = uuid4()
    content = _docx(paragraphs=["合同正文 " + ("条款内容。" * 200)], table=(("甲方", "乙方"),))
    try:
        result = _service(knowledge_db).ingest(
            _request(tenant_id, content, "docx", "合同.docx", uuid4())
        )
        extracted = extract_document(content, "docx")
        expected = chunk_text(extracted.text)
        chunks = _chunks(knowledge_db.pool, tenant_id, result.document_id)

        assert "合同正文" in extracted.text
        assert "甲方" in extracted.text and "乙方" in extracted.text
        assert [chunk.chunk_index for chunk in expected] == [row[0] for row in chunks]
        assert [chunk.content for chunk in expected] == [row[1] for row in chunks]
        assert result.chunk_count == len(expected)
        assert len(expected) > 1
        for chunk, row in zip(expected, chunks):
            metadata = _metadata(row[3])
            assert metadata["char_start"] == chunk.char_start
            assert metadata["char_end"] == chunk.char_end
            assert row[2] == len(chunk.content)
    finally:
        _delete_tenant(knowledge_db.pool, tenant_id)


def test_empty_input_writes_nothing(knowledge_db):
    tenant_id = uuid4()
    try:
        with pytest.raises(DocumentValidationError):
            _service(knowledge_db).ingest(_request(tenant_id, b"", "pdf", "empty.pdf", uuid4()))
        assert _document_count(knowledge_db.pool, tenant_id) == 0
    finally:
        _delete_tenant(knowledge_db.pool, tenant_id)


def test_pdf_without_text_writes_nothing(knowledge_db):
    tenant_id = uuid4()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    payload = BytesIO()
    writer.write(payload)
    try:
        with pytest.raises(NoExtractableTextError):
            _service(knowledge_db).ingest(
                _request(tenant_id, payload.getvalue(), "pdf", "blank.pdf", uuid4())
            )
        assert _document_count(knowledge_db.pool, tenant_id) == 0
    finally:
        _delete_tenant(knowledge_db.pool, tenant_id)


def test_same_file_and_checksum_reuses_the_document(knowledge_db):
    tenant_id = uuid4()
    file_id = uuid4()
    content = _pdf_pages(["same bytes"])
    service = _service(knowledge_db)
    try:
        first = service.ingest(_request(tenant_id, content, "pdf", "original.pdf", file_id))
        second = service.ingest(_request(tenant_id, content, "pdf", "renamed.pdf", file_id))
        stored = _document(knowledge_db.pool, tenant_id, first.document_id)

        assert second.reused is True
        assert second.document_id == first.document_id
        assert second.chunk_count == first.chunk_count
        assert stored[0] == "original.pdf"
        assert _document_count(knowledge_db.pool, tenant_id) == 1
        assert len(_chunks(knowledge_db.pool, tenant_id, first.document_id)) == first.chunk_count
    finally:
        _delete_tenant(knowledge_db.pool, tenant_id)


def test_same_file_and_new_checksum_replaces_chunks(knowledge_db):
    tenant_id = uuid4()
    file_id = uuid4()
    service = _service(knowledge_db)
    try:
        first = service.ingest(
            _request(tenant_id, _pdf_pages(["version one"]), "pdf", "v1.pdf", file_id)
        )
        second = service.ingest(
            _request(tenant_id, _pdf_pages(["version two"]), "pdf", "v2.pdf", file_id)
        )
        chunks = _chunks(knowledge_db.pool, tenant_id, first.document_id)
        stored = _document(knowledge_db.pool, tenant_id, first.document_id)

        assert second.reused is False
        assert second.document_id == first.document_id
        assert _document_count(knowledge_db.pool, tenant_id) == 1
        assert stored[0] == "v2.pdf"
        assert [row[1] for row in chunks] == ["version two"]
        assert stored[4] == 1
    finally:
        _delete_tenant(knowledge_db.pool, tenant_id)


def test_same_checksum_without_file_id_reuses_the_document(knowledge_db):
    tenant_id = uuid4()
    content = _pdf_pages(["anonymous"])
    service = _service(knowledge_db)
    try:
        first = service.ingest(_request(tenant_id, content, "pdf", "one.pdf", None))
        second = service.ingest(_request(tenant_id, content, "pdf", "two.pdf", None))

        assert second.reused is True
        assert second.document_id == first.document_id
        assert _document_count(knowledge_db.pool, tenant_id) == 1
    finally:
        _delete_tenant(knowledge_db.pool, tenant_id)


def test_different_checksum_without_file_id_creates_another_document(knowledge_db):
    tenant_id = uuid4()
    service = _service(knowledge_db)
    try:
        first = service.ingest(
            _request(tenant_id, _pdf_pages(["alpha"]), "pdf", "alpha.pdf", None)
        )
        second = service.ingest(
            _request(tenant_id, _pdf_pages(["beta"]), "pdf", "beta.pdf", None)
        )

        assert second.reused is False
        assert second.document_id != first.document_id
        assert _document_count(knowledge_db.pool, tenant_id) == 2
    finally:
        _delete_tenant(knowledge_db.pool, tenant_id)


def test_first_ingest_failure_rolls_back(knowledge_db):
    tenant_id = uuid4()
    try:
        _set_chunk_insert(knowledge_db.env, granted=False)
        try:
            with pytest.raises(InsufficientPrivilege):
                _service(knowledge_db).ingest(
                    _request(tenant_id, _pdf_pages(["rollback"]), "pdf", "rollback.pdf", uuid4())
                )
        finally:
            _set_chunk_insert(knowledge_db.env, granted=True)
        assert _document_count(knowledge_db.pool, tenant_id) == 0
    finally:
        _delete_tenant(knowledge_db.pool, tenant_id)


def test_failed_reingest_keeps_the_previous_chunks(knowledge_db):
    tenant_id = uuid4()
    file_id = uuid4()
    service = _service(knowledge_db)
    try:
        first = service.ingest(
            _request(tenant_id, _pdf_pages(["kept text"]), "pdf", "kept.pdf", file_id)
        )
        _set_chunk_insert(knowledge_db.env, granted=False)
        try:
            with pytest.raises(InsufficientPrivilege):
                service.ingest(
                    _request(tenant_id, _pdf_pages(["replacement"]), "pdf", "next.pdf", file_id)
                )
        finally:
            _set_chunk_insert(knowledge_db.env, granted=True)
        stored = _document(knowledge_db.pool, tenant_id, first.document_id)
        chunks = _chunks(knowledge_db.pool, tenant_id, first.document_id)

        assert stored[0] == "kept.pdf"
        assert [row[1] for row in chunks] == ["kept text"]
        assert stored[4] == 1
    finally:
        _delete_tenant(knowledge_db.pool, tenant_id)


def test_tenants_cannot_read_each_other(knowledge_db):
    tenant_a = uuid4()
    tenant_b = uuid4()
    service = _service(knowledge_db)
    try:
        created = service.ingest(
            _request(tenant_a, _pdf_pages(["tenant-a"]), "pdf", "a.pdf", uuid4())
        )
        with tenant_transaction(knowledge_db.pool, str(tenant_b)) as connection:
            visible = connection.execute(
                "select count(*) from agent.knowledge_document where id = %s",
                (created.document_id,),
            ).fetchone()
            visible_chunks = connection.execute(
                "select count(*) from agent.knowledge_chunk where document_id = %s",
                (created.document_id,),
            ).fetchone()
        assert visible[0] == 0
        assert visible_chunks[0] == 0
        assert _document_count(knowledge_db.pool, tenant_a) == 1
    finally:
        _delete_tenant(knowledge_db.pool, tenant_a)
        _delete_tenant(knowledge_db.pool, tenant_b)


def test_chunk_must_reference_the_same_tenant_document(knowledge_db):
    tenant_a = uuid4()
    tenant_b = uuid4()
    service = _service(knowledge_db)
    try:
        created = service.ingest(
            _request(tenant_a, _pdf_pages(["linked"]), "pdf", "linked.pdf", uuid4())
        )
        with pytest.raises(ForeignKeyViolation):
            with tenant_transaction(knowledge_db.pool, str(tenant_a)) as connection:
                connection.execute(
                    """
                    insert into agent.knowledge_chunk (
                        tenant_id, document_id, chunk_index, content, char_count, metadata
                    ) values (%s, %s, 0, 'x', 1, '{}'::jsonb)
                    """,
                    (tenant_a, uuid4()),
                )
        with pytest.raises(InsufficientPrivilege):
            with tenant_transaction(knowledge_db.pool, str(tenant_b)) as connection:
                connection.execute(
                    """
                    insert into agent.knowledge_chunk (
                        tenant_id, document_id, chunk_index, content, char_count, metadata
                    ) values (%s, %s, 0, 'x', 1, '{}'::jsonb)
                    """,
                    (tenant_a, created.document_id),
                )
        assert len(_chunks(knowledge_db.pool, tenant_a, created.document_id)) == created.chunk_count
    finally:
        _delete_tenant(knowledge_db.pool, tenant_a)
        _delete_tenant(knowledge_db.pool, tenant_b)


def test_concurrent_first_ingest_keeps_one_document(knowledge_db):
    tenant_id = uuid4()
    file_id = uuid4()
    content = _pdf_pages(["concurrent same"])
    service = _service(knowledge_db)
    barrier = threading.Barrier(2)
    outcomes: list[object] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            barrier.wait(5)
            result = service.ingest(_request(tenant_id, content, "pdf", "concurrent.pdf", file_id))
            with lock:
                outcomes.append(result)
        except Exception as exc:
            with lock:
                outcomes.append(exc)

    try:
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        results = [item for item in outcomes if not isinstance(item, Exception)]
        errors = [item for item in outcomes if isinstance(item, Exception)]

        assert errors == []
        assert len(results) == 2
        assert results[0].document_id == results[1].document_id
        assert {result.reused for result in results} == {False, True}
        assert _document_count(knowledge_db.pool, tenant_id) == 1
        assert [row[1] for row in _chunks(knowledge_db.pool, tenant_id, results[0].document_id)] == [
            "concurrent same"
        ]
    finally:
        _delete_tenant(knowledge_db.pool, tenant_id)


def test_concurrent_replacements_do_not_mix_chunks(knowledge_db):
    tenant_id = uuid4()
    file_id = uuid4()
    contents = {
        "left": _pdf_pages(["left version"]),
        "right": _pdf_pages(["right version"]),
    }
    service = _service(knowledge_db)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker(label: str) -> None:
        try:
            barrier.wait(5)
            service.ingest(_request(tenant_id, contents[label], "pdf", f"{label}.pdf", file_id))
        except Exception as exc:
            errors.append(exc)

    try:
        threads = [threading.Thread(target=worker, args=(label,)) for label in contents]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert errors == []
        assert _document_count(knowledge_db.pool, tenant_id) == 1
        stored_checksum = _only_checksum(knowledge_db.pool, tenant_id)
        winner = next(
            content for content in contents.values() if hashlib.sha256(content).hexdigest() == stored_checksum
        )
        expected = chunk_text(extract_document(winner, "pdf").text)
        chunks = _chunks_for_tenant(knowledge_db.pool, tenant_id)
        assert [row[1] for row in chunks] == [chunk.content for chunk in expected]
    finally:
        _delete_tenant(knowledge_db.pool, tenant_id)


def test_applied_migration_is_not_repeated(knowledge_db):
    apply_migrations(knowledge_db.settings)
    with _migrator(knowledge_db.env) as connection:
        row = connection.execute(
            "select count(*) from agent.schema_migrations where version = '001'"
        ).fetchone()
    assert row[0] == 1


def test_failed_migration_rolls_back(knowledge_db, tmp_path: Path):
    script = tmp_path / "901_failure.sql"
    script.write_text(
        "create table agent.migration_failure_probe (id int);\nselect 1/0;\n",
        encoding="utf-8",
    )
    with pytest.raises(DivisionByZero):
        apply_migrations(knowledge_db.settings, tmp_path)
    with _migrator(knowledge_db.env) as connection:
        probe = connection.execute("select to_regclass('agent.migration_failure_probe')").fetchone()
        version = connection.execute(
            "select count(*) from agent.schema_migrations where version = '901'"
        ).fetchone()
    assert probe[0] is None
    assert version[0] == 0


def test_app_role_cannot_write_migration_history(knowledge_db):
    with pytest.raises(InsufficientPrivilege):
        with knowledge_db.pool.connection() as connection:
            connection.execute(
                "insert into agent.schema_migrations (version, name) values ('999', 'blocked')"
            )


def test_knowledge_tables_force_row_level_security(knowledge_db):
    with knowledge_db.pool.connection() as connection:
        rows = connection.execute(
            """
            select c.relname, c.relrowsecurity, c.relforcerowsecurity
            from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'agent'
              and c.relname in ('knowledge_document', 'knowledge_chunk')
            order by c.relname
            """
        ).fetchall()
    assert [(row[0], row[1], row[2]) for row in rows] == [
        ("knowledge_chunk", True, True),
        ("knowledge_document", True, True),
    ]


@pytest.fixture(scope="module")
def knowledge_db():
    env = _root_env()
    if (
        not env.get("AGENT_APP_PASSWORD")
        or not env.get("AGENT_MIGRATOR_PASSWORD")
        or not env.get("POSTGRES_DB")
    ):
        pytest.skip(
            "仓库根目录 .env 未提供 AGENT_APP_PASSWORD 或 AGENT_MIGRATOR_PASSWORD，跳过真实数据库测试"
        )
    settings = _settings(env)
    try:
        apply_migrations(settings)
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL 不可用，跳过真实数据库测试: {exc.__class__.__name__}")
    conninfo = build_conninfo(
        settings.postgres_host,
        settings.postgres_port,
        settings.postgres_db,
        settings.agent_app_user,
        settings.agent_app_password,
    )
    pool = ConnectionPool(
        conninfo,
        min_size=2,
        max_size=4,
        timeout=10,
        kwargs={"options": "-c search_path=agent"},
        open=True,
    )
    try:
        yield type("KnowledgeDatabase", (), {"pool": pool, "env": env, "settings": settings})()
    finally:
        pool.close()


def _service(knowledge_db) -> KnowledgeIngestionService:
    return KnowledgeIngestionService(knowledge_db.pool)


def _request(
    tenant_id: UUID,
    content: bytes,
    file_type: str,
    document_name: str,
    file_id: UUID | None,
) -> IngestDocument:
    return IngestDocument(
        tenant_id=tenant_id,
        content=content,
        file_type=file_type,
        document_name=document_name,
        file_id=file_id,
    )


def _settings(env: dict[str, str]) -> Settings:
    return Settings(
        postgres_host=env.get("POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(env.get("POSTGRES_PORT", "5432")),
        postgres_db=env["POSTGRES_DB"],
        agent_app_user="agent_app",
        agent_app_password=env["AGENT_APP_PASSWORD"],
        agent_migrator_user="agent_migrator",
        agent_migrator_password=env["AGENT_MIGRATOR_PASSWORD"],
    )


def _root_env() -> dict[str, str]:
    path = Path(__file__).resolve().parents[2] / ".env"
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _migrator(env: dict[str, str]) -> Connection:
    settings = _settings(env)
    return Connection.connect(
        build_conninfo(
            settings.postgres_host,
            settings.postgres_port,
            settings.postgres_db,
            settings.agent_migrator_user,
            settings.agent_migrator_password,
        ),
        options="-c search_path=agent",
    )


def _set_chunk_insert(env: dict[str, str], *, granted: bool) -> None:
    verb = "grant" if granted else "revoke"
    direction = "to" if granted else "from"
    with _migrator(env) as connection:
        connection.execute(
            f"{verb} insert on table agent.knowledge_chunk {direction} agent_app"
        )
        connection.commit()


def _document(pool: ConnectionPool, tenant_id: UUID, document_id: UUID):
    with tenant_transaction(pool, str(tenant_id)) as connection:
        return connection.execute(
            """
            select document_name, file_type, file_checksum, status, total_chunks
            from agent.knowledge_document
            where id = %s
            """,
            (document_id,),
        ).fetchone()


def _chunks(pool: ConnectionPool, tenant_id: UUID, document_id: UUID):
    with tenant_transaction(pool, str(tenant_id)) as connection:
        return connection.execute(
            """
            select chunk_index, content, char_count, metadata
            from agent.knowledge_chunk
            where document_id = %s
            order by chunk_index
            """,
            (document_id,),
        ).fetchall()


def _chunks_for_tenant(pool: ConnectionPool, tenant_id: UUID):
    with tenant_transaction(pool, str(tenant_id)) as connection:
        return connection.execute(
            """
            select chunk_index, content
            from agent.knowledge_chunk
            order by chunk_index
            """
        ).fetchall()


def _only_checksum(pool: ConnectionPool, tenant_id: UUID) -> str:
    with tenant_transaction(pool, str(tenant_id)) as connection:
        row = connection.execute("select file_checksum from agent.knowledge_document").fetchone()
    return row[0]


def _document_count(pool: ConnectionPool, tenant_id: UUID) -> int:
    with tenant_transaction(pool, str(tenant_id)) as connection:
        row = connection.execute(
            "select count(*) from agent.knowledge_document where tenant_id = %s",
            (tenant_id,),
        ).fetchone()
    return int(row[0])


def _delete_tenant(pool: ConnectionPool, tenant_id: UUID) -> None:
    with tenant_transaction(pool, str(tenant_id)) as connection:
        connection.execute(
            "delete from agent.knowledge_document where tenant_id = %s",
            (tenant_id,),
        )


def _assert_tenant_does_not_leak(pool: ConnectionPool) -> None:
    with pool.connection() as connection:
        row = connection.execute("select current_setting('app.tenant_id', true)").fetchone()
    assert row[0] == ""


def _metadata(value: object) -> dict[str, object]:
    if isinstance(value, str):
        loaded = json.loads(value)
    else:
        loaded = value
    assert isinstance(loaded, dict)
    return loaded
