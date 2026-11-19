"""Search knowledge chunks by overlapping character pairs.

This is not Chinese word segmentation and it does not use pg_trgm. PostgreSQL
counts how many query terms occur in a chunk. Terms are adjacent character
pairs from CJK text, plus ASCII and digit tokens. Rows are limited to one
tenant inside tenant_transaction, and RLS stays in force.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from psycopg_pool import ConnectionPool

from app.knowledge.retrieval import MAX_TOP_K
from app.persistence.postgres import tenant_transaction

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_TOKEN_RUN = re.compile(r"[A-Za-z0-9]+")
_MIN_MATCHES = 1

_SEARCH_SQL = """
select
    c.id,
    c.document_id,
    c.chunk_index,
    c.content,
    c.metadata ->> 'char_start' as char_start,
    c.metadata ->> 'char_end' as char_end,
    hit.matched
from agent.knowledge_chunk c
join lateral (
    select count(distinct term)::int as matched
    from unnest(%s::text[]) as term
    where position(term in c.content) > 0
) as hit on true
where c.tenant_id = %s
  and hit.matched >= %s
  and (%s::uuid is null or c.document_id = %s)
  and (
        cardinality(%s::text[]) = 0
        or agent.character_bigrams(c.content) && %s::text[]
      )
order by hit.matched desc, c.id asc
limit %s
"""


class KeywordValidationError(ValueError):
    """The keyword query itself is not acceptable."""


@dataclass(frozen=True)
class KeywordHit:
    document_id: UUID
    chunk_id: UUID
    chunk_index: int
    content: str
    char_start: int | None
    char_end: int | None
    matched_terms: int
    score: float


class KeywordRetriever:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def search(
        self,
        tenant_id: UUID,
        query: str,
        *,
        top_k: int = 5,
        document_id: UUID | None = None,
    ) -> list[KeywordHit]:
        tenant = _require_tenant(tenant_id)
        question = _require_query(query)
        limit = _require_top_k(top_k)
        document = _require_document_id(document_id)
        terms = keyword_terms(question)
        if not terms:
            return []
        pairs = [term for term in terms if len(term) == 2]
        with tenant_transaction(self._pool, str(tenant)) as connection:
            rows = connection.execute(
                _SEARCH_SQL,
                (list(terms), tenant, _MIN_MATCHES, document, document, pairs, pairs, limit),
            ).fetchall()
        total = len(terms)
        return [_hit_from_row(row, total) for row in rows]


def keyword_terms(query: str) -> tuple[str, ...]:
    """Return deduplicated terms in first-seen order.

    A query that is shorter than two CJK characters or two ASCII characters
    produces no terms. Callers treat that as an empty result, not an error.
    """
    if not isinstance(query, str):
        return ()
    found: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        if len(term) < 2 or term in seen:
            return
        seen.add(term)
        found.append(term)

    for run in _CJK_RUN.findall(query):
        for index in range(len(run) - 1):
            add(run[index : index + 2])
    for run in _TOKEN_RUN.findall(query):
        add(run)
        for index in range(len(run) - 1):
            add(run[index : index + 2])
    return tuple(found)


def _hit_from_row(row: tuple[object, ...], term_count: int) -> KeywordHit:
    chunk_id, document_id, chunk_index, content, char_start, char_end, matched = row
    if not isinstance(chunk_id, UUID) or not isinstance(document_id, UUID):
        raise KeywordValidationError("关键词检索结果标识不正确")
    if type(chunk_index) is not int or not isinstance(content, str) or type(matched) is not int:
        raise KeywordValidationError("关键词检索结果格式不正确")
    if matched < 1 or term_count < 1:
        raise KeywordValidationError("关键词检索结果分数不正确")
    return KeywordHit(
        document_id=document_id,
        chunk_id=chunk_id,
        chunk_index=chunk_index,
        content=content,
        char_start=_offset(char_start),
        char_end=_offset(char_end),
        matched_terms=matched,
        score=matched / term_count,
    )


def _offset(value: object) -> int | None:
    if not isinstance(value, str) or not value.isdecimal():
        return None
    number = int(value)
    if number > 2_147_483_647:
        return None
    return number


def _require_tenant(tenant_id: object) -> UUID:
    if not isinstance(tenant_id, UUID):
        raise KeywordValidationError("tenant_id 必须是 UUID")
    return tenant_id


def _require_query(query: object) -> str:
    if not isinstance(query, str) or not query.strip():
        raise KeywordValidationError("查询内容不能为空")
    return query.strip()


def _require_top_k(top_k: object) -> int:
    if type(top_k) is not int or not 1 <= top_k <= MAX_TOP_K:
        raise KeywordValidationError(f"top_k 必须是 1 到 {MAX_TOP_K} 之间的整数")
    return top_k


def _require_document_id(document_id: object) -> UUID | None:
    if document_id is None:
        return None
    if not isinstance(document_id, UUID):
        raise KeywordValidationError("document_id 必须是 UUID")
    return document_id
