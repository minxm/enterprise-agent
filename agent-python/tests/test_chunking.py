import pytest

from app.knowledge.chunking import ChunkingError, chunk_text


def test_short_text_is_one_chunk():
    chunks = chunk_text("  hello  ")

    assert len(chunks) == 1
    assert chunks[0].chunk_index == 0
    assert chunks[0].content == "hello"
    assert chunks[0].char_start == 2
    assert chunks[0].char_end == 7
    assert "  hello  "[chunks[0].char_start : chunks[0].char_end] == chunks[0].content


def test_paragraph_boundary_wins_over_a_later_space():
    first = "甲" * 20
    second = "乙 " * 20
    text = first + "\n\n" + second

    chunks = chunk_text(text, chunk_size=50, chunk_overlap=0)

    assert chunks[0].content == first
    assert "乙" not in chunks[0].content
    assert chunks[1].content == second.strip()
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))


def test_multiple_paragraphs_keep_order_and_offsets():
    text = "第一段。\n\n第二段。\n\n第三段。"

    chunks = chunk_text(text, chunk_size=8, chunk_overlap=0)

    assert [chunk.content for chunk in chunks] == ["第一段。", "第二段。", "第三段。"]
    assert [chunk.chunk_index for chunk in chunks] == [0, 1, 2]
    for chunk in chunks:
        assert text[chunk.char_start : chunk.char_end] == chunk.content


def test_long_sentence_is_cut_on_a_hard_boundary():
    text = "甲" * 100

    chunks = chunk_text(text, chunk_size=30, chunk_overlap=0)

    assert [chunk.content for chunk in chunks] == ["甲" * 30, "甲" * 30, "甲" * 30, "甲" * 10]
    assert all(len(chunk.content) <= 30 for chunk in chunks)
    assert "".join(chunk.content for chunk in chunks) == text


def test_overlap_repeats_the_trailing_characters():
    text = "A" * 100

    chunks = chunk_text(text, chunk_size=40, chunk_overlap=10)

    assert chunks[0].char_start == 0
    assert chunks[0].char_end == 40
    assert chunks[1].char_start == 30
    assert chunks[1].char_end == 70
    assert chunks[1].char_start < chunks[0].char_end
    assert text[chunks[1].char_start : chunks[1].char_end] == chunks[1].content


def test_sentence_punctuation_is_kept():
    text = ("A" * 20) + ". " + ("B" * 40)

    chunks = chunk_text(text, chunk_size=30, chunk_overlap=0)

    assert chunks[0].content == ("A" * 20) + "."
    assert "B" not in chunks[0].content


def test_tiny_leading_separator_does_not_become_its_own_chunk():
    text = "。\n\n" + ("正文" * 80)

    chunks = chunk_text(text, chunk_size=40, chunk_overlap=0)

    assert len(chunks[0].content) == 40
    assert all(len(chunk.content) <= 40 for chunk in chunks)
    assert chunks[-1].char_end <= len(text)


def test_empty_fragment_is_discarded():
    text = "\n\nabcd"

    chunks = chunk_text(text, chunk_size=5, chunk_overlap=0)

    assert [chunk.content for chunk in chunks] == ["abcd"]
    assert chunks[0].char_start == 2
    assert chunks[0].char_end == 6


def test_empty_text_is_rejected():
    with pytest.raises(ChunkingError, match="不能为空"):
        chunk_text("")
    with pytest.raises(ChunkingError, match="不能为空"):
        chunk_text(" \n\t")


def test_invalid_parameters_are_rejected():
    with pytest.raises(ChunkingError):
        chunk_text("hello", chunk_size=0, chunk_overlap=0)
    with pytest.raises(ChunkingError):
        chunk_text("hello", chunk_size=800, chunk_overlap=800)
    with pytest.raises(ChunkingError):
        chunk_text("hello", chunk_size=10, chunk_overlap=-1)
    with pytest.raises(ChunkingError):
        chunk_text("hello", chunk_size=True, chunk_overlap=0)  # type: ignore[arg-type]


def test_every_chunk_stays_within_the_size_limit():
    text = ("第一句。 " * 50) + ("\n\n第二段 " * 40) + ("Z" * 500)

    chunks = chunk_text(text, chunk_size=80, chunk_overlap=15)

    assert chunks
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    for previous, current in zip(chunks, chunks[1:]):
        assert current.char_start > previous.char_start
    for chunk in chunks:
        assert 0 < len(chunk.content) <= 80
        assert text[chunk.char_start : chunk.char_end] == chunk.content
        assert chunk.content == chunk.content.strip()
