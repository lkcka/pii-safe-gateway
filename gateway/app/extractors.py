# gateway/app/extractors.py
"""
Извлечение текста из файлов (TXT, DOCX, PDF).
OCR для сканов не поддерживается — возвращаем только текстовый слой.
"""
import base64
import binascii
import io
import logging
from pathlib import Path
from typing import Iterator, Union

import docx
import pypdf
from docx.document import Document as DocxDocument
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

logger = logging.getLogger("gateway.extractors")

BlockItem = Union[Paragraph, Table]


def extract_text(filename: str, content_base64: str) -> str:
    """
    Декодирует Base64 и извлекает текст в зависимости от расширения файла.
    Возвращает plain text.
    """
    try:
        file_bytes = base64.b64decode(content_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        logger.error("Invalid Base64 for file %s: %s", filename, exc)
        raise ValueError(f"Invalid Base64 in file {filename}: {exc}") from exc

    ext = Path(filename).suffix.lower()

    if ext == ".txt":
        return _extract_txt(file_bytes)
    if ext == ".docx":
        return _extract_docx(file_bytes, filename)
    if ext == ".pdf":
        return _extract_pdf(file_bytes, filename)
    raise ValueError(f"Unsupported file format: {ext}. Allowed: .txt, .docx, .pdf")


def _extract_txt(file_bytes: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return file_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return file_bytes.decode("cp1251", errors="replace")


def _iter_block_items(parent: Union[DocxDocument, _Cell]) -> Iterator[BlockItem]:
    """
    Yield paragraphs and tables in document order.
    """
    if isinstance(parent, DocxDocument):
        parent_elm = parent.element.body
    elif isinstance(parent, _Cell):
        parent_elm = parent._tc
    else:
        raise ValueError(f"Unsupported parent type: {type(parent)!r}")

    for child in parent_elm.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)


def _extract_table_text(table: Table) -> str:
    rows: list[str] = []
    for row in table.rows:
        cells = [cell.text for cell in row.cells]
        rows.append("\t".join(cells))
    return "\n".join(rows)


def _extract_docx(file_bytes: bytes, filename: str) -> str:
    try:
        doc = docx.Document(io.BytesIO(file_bytes))
        blocks: list[str] = []

        for block in _iter_block_items(doc):
            if isinstance(block, Paragraph):
                blocks.append(block.text)
            elif isinstance(block, Table):
                blocks.append(_extract_table_text(block))

        return "\n\n".join(blocks)
    except Exception as exc:
        logger.error("Failed to parse DOCX %s: %s", filename, exc)
        raise ValueError(f"Corrupted or unsupported DOCX file: {filename}") from exc


def _extract_pdf(file_bytes: bytes, filename: str) -> str:
    try:
        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
        if reader.is_encrypted:
            raise ValueError("Encrypted PDF not supported")

        pages_text: list[str] = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text()
            if text and text.strip():
                pages_text.append(text)
            else:
                logger.warning(
                    "Page %d in PDF %s has no text layer (possibly a scan)",
                    i + 1,
                    filename,
                )

        if not pages_text:
            logger.warning("PDF %s contains no extractable text", filename)
            return ""

        return "\n\n".join(pages_text)
    except ValueError:
        raise
    except Exception as exc:
        logger.error("Failed to parse PDF %s: %s", filename, exc)
        raise ValueError(f"Corrupted or unsupported PDF file: {filename}") from exc
