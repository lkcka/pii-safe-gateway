"""Edge-case unit tests for PDF extraction, chunking, and anonymizer overlap resolution."""
from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest
import pypdf
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.anonymizer import anonymize_text
from app.chunker import chunk_text
from app.extractors import extract_text
from app.schemas import GlobalEntity


def _unicode_font_path() -> Path | None:
    """Return first available system font that supports Cyrillic."""
    candidates = [
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"),
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def _encode_pdf(writer: PdfWriter) -> str:
    buffer = io.BytesIO()
    writer.write(buffer)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _add_text_layer_to_page(writer: PdfWriter, page: pypdf.PageObject, text: str) -> None:
    """Attach an extractable text layer to a PdfWriter page."""
    if all(ord(char) < 128 for char in text):
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        font_ref = writer._add_object(font)
        resources = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
        )
        page[NameObject("/Resources")] = writer._add_object(resources)

        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 100 700 Td ({escaped}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
        return

    fpdf = pytest.importorskip("fpdf")
    font_path = _unicode_font_path()
    if font_path is None:
        pytest.skip("No Unicode TTF font available for Cyrillic PDF test")

    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.add_font("TestFont", "", str(font_path))
    pdf.set_font("TestFont", size=14)
    pdf.text(20, 30, text)

    overlay_reader = pypdf.PdfReader(io.BytesIO(pdf.output()))
    page.merge_page(overlay_reader.pages[0])


def _make_pdf_bytes(
    *,
    text: str | None = None,
    encrypted: bool = False,
    blank_page: bool = False,
) -> str:
    """Build an in-memory PDF via pypdf.PdfWriter and return Base64 payload."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)

    if not blank_page:
        assert text is not None
        _add_text_layer_to_page(writer, page, text)

    if encrypted:
        writer.encrypt("password")

    return _encode_pdf(writer)


class TestPdfExtraction:
    def test_extract_valid_pdf(self) -> None:
        expected_text = "Тестовый текст"
        content_base64 = _make_pdf_bytes(text=expected_text)

        extracted = extract_text("document.pdf", content_base64)

        assert expected_text in extracted
        assert extracted.strip() == expected_text

    def test_extract_encrypted_pdf(self) -> None:
        content_base64 = _make_pdf_bytes(text="Secret", encrypted=True)

        with pytest.raises(ValueError, match="Encrypted PDF not supported"):
            extract_text("encrypted.pdf", content_base64)

    def test_extract_scanned_pdf(self) -> None:
        content_base64 = _make_pdf_bytes(blank_page=True)

        extracted = extract_text("scan.pdf", content_base64)

        assert extracted == ""


class TestChunkingLogic:
    def test_chunking_overlap_indices(self) -> None:
        text = "a" * 5000
        max_chars = 2000
        overlap_chars = 200

        chunks = chunk_text(text, max_chars=max_chars, overlap_chars=overlap_chars)

        assert len(chunks) == 3

        for chunk in chunks:
            assert text[chunk.start_idx : chunk.end_idx] == chunk.text

        for previous, current in zip(chunks[:-1], chunks[1:], strict=True):
            assert previous.end_idx - overlap_chars == current.start_idx

        reconstructed = chunks[0].text
        for chunk in chunks[1:]:
            reconstructed += chunk.text[overlap_chars:]

        assert reconstructed == text


class TestAnonymizerOverlapResolution:
    def test_deduplication_from_overlap(self) -> None:
        original_text = "x" * 50 + "Иванов" + "y" * 44
        assert len(original_text) == 100

        duplicate_entities = [
            GlobalEntity(start=50, end=56, text="Иванов", type="PERSON"),
            GlobalEntity(start=51, end=57, text="Иванов", type="PERSON"),
        ]

        anonymized, applied = anonymize_text(
            original_text,
            [entity.model_dump() for entity in duplicate_entities],
        )

        assert len(applied) == 1
        assert anonymized.count("[PERSON]") == 1
        assert "Иванов" not in anonymized
        assert applied[0].start == 50
        assert applied[0].end == 56

    def test_entity_on_chunk_boundary(self) -> None:
        # Entity starts exactly where a hypothetical chunk boundary would be.
        original_text = "A" * 50 + "Иванов" + "B" * 44
        entity = GlobalEntity(start=50, end=56, text="Иванов", type="PERSON")

        anonymized, applied = anonymize_text(original_text, [entity.model_dump()])

        assert len(applied) == 1
        assert anonymized.count("[PERSON]") == 1
        assert original_text[:50] in anonymized
        assert original_text[56:] in anonymized
        assert "Иванов" not in anonymized
