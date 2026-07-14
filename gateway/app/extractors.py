# gateway/app/extractors.py
"""
Извлечение текста из файлов (TXT, DOCX, PDF).
OCR для сканов не поддерживается — возвращаем только текстовый слой.
"""
import base64
import io
import logging
from pathlib import Path

import pypdf
import docx

logger = logging.getLogger("gateway.extractors")

def extract_text(filename: str, content_base64: str) -> str:
    """
    Декодирует Base64 и извлекает текст в зависимости от расширения файла.
    Возвращает plain text.
    """
    try:
        file_bytes = base64.b64decode(content_base64)
    except Exception as e:
        logger.error("Ошибка декодирования Base64 для файла %s: %s", filename, e)
        raise ValueError(f"Невалидный Base64 в файле {filename}")

    ext = Path(filename).suffix.lower()

    if ext == ".txt":
        return _extract_txt(file_bytes)
    elif ext == ".docx":
        return _extract_docx(file_bytes, filename)
    elif ext == ".pdf":
        return _extract_pdf(file_bytes, filename)
    else:
        raise ValueError(f"Неподдерживаемый формат файла: {ext}. Разрешены: .txt, .docx, .pdf")

def _extract_txt(file_bytes: bytes) -> str:
    # Пытаемся декодировать как UTF-8, с fallback на Windows-1251
    try:
        return file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return file_bytes.decode("cp1251", errors="replace")

def _extract_docx(file_bytes: bytes, filename: str) -> str:
    try:
        doc = docx.Document(io.BytesIO(file_bytes))
        # Извлекаем текст из параграфов. 
        # Примечание: таблицы в базовой реализации игнорируются для простоты, 
        # но структура параграфов сохраняется.
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n\n".join(paragraphs)
    except Exception as e:
        logger.error("Ошибка парсинга DOCX %s: %s", filename, e)
        raise ValueError(f"Поврежденный или неподдерживаемый DOCX файл: {filename}")

def _extract_pdf(file_bytes: bytes, filename: str) -> str:
    try:
        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
        pages_text = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text()
            if text and text.strip():
                pages_text.append(text)
            else:
                logger.warning("Страница %d в PDF %s не содержит текстового слоя (возможно, скан)", i + 1, filename)
        
        if not pages_text:
            logger.warning("PDF %s не содержит извлекаемого текста", filename)
            return ""
            
        return "\n\n".join(pages_text)
    except Exception as e:
        logger.error("Ошибка парсинга PDF %s: %s", filename, e)
        raise ValueError(f"Поврежденный или неподдерживаемый PDF файл: {filename}")