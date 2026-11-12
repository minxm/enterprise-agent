"""Extract plain text from PDF and DOCX bytes.

This module does not fetch files, talk to storage, or build embeddings.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from io import BytesIO
from typing import Iterator
from zipfile import ZipFile

from docx import Document
from docx.document import Document as DocxDocument
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader
from pypdf.errors import PdfReadError

MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
_PDF_MAGIC = b"%PDF"
_DOCX_ENTRY = "word/document.xml"


class DocumentValidationError(ValueError):
    """The bytes or declared type cannot be accepted for parsing."""


class UnsupportedDocumentTypeError(ValueError):
    """The caller asked for a type this parser does not implement."""


class DocumentParseError(RuntimeError):
    """The file looks like the declared type but cannot be read."""


class NoExtractableTextError(RuntimeError):
    """The file opened, but it contains no text. OCR is intentionally not used."""


@dataclass(frozen=True)
class DocumentExtractionResult:
    text: str
    file_type: str
    page_count: int | None
    paragraph_count: int | None


def extract_document(content: bytes, file_type: str) -> DocumentExtractionResult:
    normalized = _normalize_file_type(file_type)
    _validate_content(content)
    if normalized == "pdf":
        _require_pdf(content)
        return _extract_pdf(content)
    _require_docx(content)
    return _extract_docx(content)


def _normalize_file_type(file_type: str) -> str:
    if not isinstance(file_type, str):
        raise DocumentValidationError("file_type 必须是字符串")
    normalized = file_type.strip().lower()
    if normalized not in {"pdf", "docx"}:
        raise UnsupportedDocumentTypeError("仅支持 pdf 和 docx")
    return normalized


def _validate_content(content: bytes) -> None:
    if not isinstance(content, bytes):
        raise DocumentValidationError("content 必须是 bytes")
    if len(content) == 0:
        raise DocumentValidationError("content 不能为空")
    if len(content) > MAX_DOCUMENT_BYTES:
        raise DocumentValidationError("文件大小超出限制")


def _require_pdf(content: bytes) -> None:
    if not content.startswith(_PDF_MAGIC):
        raise DocumentValidationError("文件内容与声明的类型不一致")


def _require_docx(content: bytes) -> None:
    if not content.startswith(b"PK"):
        raise DocumentValidationError("文件内容与声明的类型不一致")
    try:
        with ZipFile(BytesIO(content)) as package:
            names = [name.replace("\\", "/") for name in package.namelist()]
    except zipfile.BadZipFile as exc:
        raise DocumentParseError("DOCX 无法解析") from exc
    if any(_unsafe_zip_name(name) for name in names):
        raise DocumentParseError("DOCX 无法解析")
    if _DOCX_ENTRY not in names:
        raise DocumentValidationError("DOCX 缺少 word/document.xml")


def _unsafe_zip_name(name: str) -> bool:
    parts = name.split("/")
    return name.startswith("/") or ".." in parts


def _extract_pdf(content: bytes) -> DocumentExtractionResult:
    try:
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted:
            # Day 15 does not accept passwords and does not attempt to decrypt.
            raise DocumentParseError("PDF 已加密，无法提取文本")
        parts: list[str] = []
        for page in reader.pages:
            extracted = page.extract_text() or ""
            text = extracted.strip()
            if text:
                parts.append(text)
        page_count = len(reader.pages)
    except DocumentParseError:
        raise
    except (PdfReadError, OSError, ValueError, TypeError) as exc:
        raise DocumentParseError("PDF 无法解析") from exc
    if not parts:
        raise NoExtractableTextError("PDF 没有可提取的文本")
    return DocumentExtractionResult(
        text="\n".join(parts),
        file_type="pdf",
        page_count=page_count,
        paragraph_count=None,
    )


def _extract_docx(content: bytes) -> DocumentExtractionResult:
    try:
        document = Document(BytesIO(content))
        parts: list[str] = []
        paragraph_count = 0
        for block in _iter_blocks(document):
            if isinstance(block, Paragraph):
                paragraph_count += 1
                text = block.text.strip()
                if text:
                    parts.append(text)
                continue
            for row in block.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    parts.append("\t".join(cells))
    except (NoExtractableTextError, DocumentParseError):
        raise
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        raise DocumentParseError("DOCX 无法解析") from exc
    if not parts:
        raise NoExtractableTextError("DOCX 没有可提取的文本")
    return DocumentExtractionResult(
        text="\n".join(parts),
        file_type="docx",
        page_count=None,
        paragraph_count=paragraph_count,
    )


def _iter_blocks(document: DocxDocument) -> Iterator[Paragraph | Table]:
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)
