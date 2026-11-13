"""Split extracted document text into bounded chunks.

The splitter counts characters. It does not call an embedding model or a tokenizer.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 120

# Highest priority first. Inside one group, the rightmost usable cut wins.
_SEPARATORS: tuple[tuple[str, ...], ...] = (
    ("\n\n",),
    ("\n",),
    ("。", "！", "？", "；"),
    (". ", "! ", "? ", "; "),
    (" ",),
)


class ChunkingError(ValueError):
    """The text or chunk parameters cannot be split."""


@dataclass(frozen=True)
class TextChunk:
    chunk_index: int
    content: str
    char_start: int
    char_end: int


def chunk_text(
    text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[TextChunk]:
    _validate_parameters(chunk_size, chunk_overlap)
    if not isinstance(text, str) or not text.strip():
        raise ChunkingError("文本不能为空")

    minimum_cut = _minimum_cut(chunk_size)
    chunks: list[TextChunk] = []
    start = 0
    length = len(text)
    while start < length:
        if length - start <= chunk_size:
            cut = length
        else:
            window_cut = _find_cut(text[start : start + chunk_size], minimum_cut)
            cut = start + window_cut
        content_start, content_end = _trim_bounds(text, start, cut)
        if content_start < content_end:
            content = text[content_start:content_end]
            chunks.append(
                TextChunk(
                    chunk_index=len(chunks),
                    content=content,
                    char_start=content_start,
                    char_end=content_end,
                )
            )
        if cut >= length:
            break
        next_start = cut - chunk_overlap
        # A boundary shorter than the overlap must still move forward.
        if next_start <= start:
            next_start = cut
        start = next_start
    if not chunks:
        raise ChunkingError("文本不能为空")
    return chunks


def _validate_parameters(chunk_size: int, chunk_overlap: int) -> None:
    if type(chunk_size) is not int or type(chunk_overlap) is not int:
        raise ChunkingError("chunk_size 和 chunk_overlap 必须是整数")
    if chunk_size < 1 or not 0 <= chunk_overlap < chunk_size:
        raise ChunkingError("必须满足 0 <= chunk_overlap < chunk_size")


def _minimum_cut(chunk_size: int) -> int:
    """Ignore a boundary that would keep only a tiny prefix of the window."""
    return max(1, min(64, chunk_size // 4))


def _find_cut(window: str, minimum_cut: int) -> int:
    """Return an exclusive end offset inside window.

    The first separator group that has a cut at or after minimum_cut wins.
    Within that group the rightmost cut is used. Otherwise the window is cut
    at its end.
    """
    limit = len(window)
    for group in _SEPARATORS:
        best = -1
        for separator in group:
            begin = 0
            while True:
                found = window.find(separator, begin)
                if found < 0:
                    break
                cut = found + len(separator)
                if minimum_cut <= cut <= limit and cut > best:
                    best = cut
                begin = found + 1
        if best != -1:
            return best
    return limit


def _trim_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end
