import inspect
import zipfile
from io import BytesIO

import pytest
from docx import Document
from pypdf import PdfWriter

from app.documents.extract import (
    MAX_DOCUMENT_BYTES,
    DocumentExtractionResult,
    DocumentParseError,
    DocumentValidationError,
    NoExtractableTextError,
    UnsupportedDocumentTypeError,
    extract_document,
)


def test_pdf_text_is_extracted():
    result = extract_document(_pdf_pages(["Contract alpha"]), "pdf")

    assert result == DocumentExtractionResult(
        text="Contract alpha",
        file_type="pdf",
        page_count=1,
        paragraph_count=None,
    )


def test_multipage_pdf_keeps_every_page():
    result = extract_document(_pdf_pages(["page-one", "page-two"]), "PDF")

    assert result.text == "page-one\npage-two"
    assert result.page_count == 2
    assert result.paragraph_count is None


def test_empty_pdf_input_is_rejected():
    with pytest.raises(DocumentValidationError, match="不能为空"):
        extract_document(b"", "pdf")


def test_damaged_pdf_is_rejected():
    with pytest.raises(DocumentParseError, match="PDF 无法解析"):
        extract_document(b"%PDF-1.4\nbroken\n%%EOF\n", "pdf")


def test_encrypted_pdf_is_rejected():
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt("secret")
    payload = BytesIO()
    writer.write(payload)

    with pytest.raises(DocumentParseError, match="已加密"):
        extract_document(payload.getvalue(), "pdf")


def test_pdf_without_text_is_rejected():
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    payload = BytesIO()
    writer.write(payload)

    with pytest.raises(NoExtractableTextError):
        extract_document(payload.getvalue(), "pdf")


def test_oversized_document_is_rejected():
    payload = b"%PDF" + b"0" * MAX_DOCUMENT_BYTES

    with pytest.raises(DocumentValidationError, match="超出限制"):
        extract_document(payload, "pdf")


def test_docx_paragraph_is_extracted():
    result = extract_document(_docx(paragraphs=["合同正文"]), "docx")

    assert "合同正文" in result.text
    assert result.file_type == "docx"
    assert result.page_count is None
    assert result.paragraph_count is not None
    assert result.paragraph_count >= 1


def test_docx_table_text_is_extracted():
    result = extract_document(_docx(table=(("甲方", "乙方"),)), "docx")

    assert "甲方" in result.text
    assert "乙方" in result.text
    assert "\t" in result.text


def test_empty_docx_input_is_rejected():
    with pytest.raises(DocumentValidationError, match="不能为空"):
        extract_document(b"", "docx")


def test_docx_without_document_xml_is_rejected():
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w") as package:
        package.writestr("word/other.xml", "<root/>")

    with pytest.raises(DocumentValidationError, match="document.xml"):
        extract_document(payload.getvalue(), "docx")


def test_damaged_docx_is_rejected():
    with pytest.raises(DocumentParseError, match="DOCX 无法解析"):
        extract_document(b"PK\x03\x04not-a-zip", "docx")


def test_unsupported_type_is_rejected():
    with pytest.raises(UnsupportedDocumentTypeError):
        extract_document(b"%PDF-1.4", "txt")
    with pytest.raises(UnsupportedDocumentTypeError):
        extract_document(b"\xd0\xcf\x11\xe0", "doc")


def test_declared_type_must_match_content():
    pdf = _pdf_pages(["only-pdf"])
    docx = _docx(paragraphs=["only-docx"])

    with pytest.raises(DocumentValidationError, match="不一致"):
        extract_document(pdf, "docx")
    with pytest.raises(DocumentValidationError, match="不一致"):
        extract_document(docx, "pdf")


def test_non_bytes_input_is_rejected():
    with pytest.raises(DocumentValidationError, match="bytes"):
        extract_document("not-bytes", "pdf")  # type: ignore[arg-type]
    with pytest.raises(DocumentValidationError, match="bytes"):
        extract_document(bytearray(b"%PDF"), "pdf")  # type: ignore[arg-type]


def test_result_exposes_the_agreed_fields():
    result = extract_document(_pdf_pages(["fields"]), "pdf")

    assert set(DocumentExtractionResult.__dataclass_fields__) == {
        "text",
        "file_type",
        "page_count",
        "paragraph_count",
    }
    assert isinstance(result.text, str)
    assert result.file_type == "pdf"


def test_parser_does_not_depend_on_storage_or_services():
    import app.documents.extract as extract

    source = inspect.getsource(extract)
    for name in ("minio", "psycopg", "redis", "fastapi", "httpx", "sqlalchemy"):
        assert name not in source


def _pdf_pages(texts: list[str]) -> bytes:
    objects: list[bytes] = []
    font_id = 3 + len(texts) * 2
    kids = " ".join(f"{3 + index * 2} 0 R" for index in range(len(texts)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(texts)} >>".encode())
    for index, text in enumerate(texts):
        content_id = 4 + index * 2
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Contents {content_id} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> >>"
            ).encode()
        )
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    header = b"%PDF-1.4\n"
    body = b""
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(header) + len(body))
        body += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_at = len(header) + len(body)
    xref = [f"xref\n0 {len(objects) + 1}\n".encode(), b"0000000000 65535 f \n"]
    xref.extend(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return header + body + b"".join(xref) + trailer


def _docx(*, paragraphs: list[str] | None = None, table: tuple[tuple[str, ...], ...] | None = None) -> bytes:
    document = Document()
    for paragraph in paragraphs or []:
        document.add_paragraph(paragraph)
    if table is not None:
        rows = document.add_table(rows=len(table), cols=len(table[0]))
        for row_index, row in enumerate(table):
            for column_index, value in enumerate(row):
                rows.cell(row_index, column_index).text = value
    payload = BytesIO()
    document.save(payload)
    return payload.getvalue()
