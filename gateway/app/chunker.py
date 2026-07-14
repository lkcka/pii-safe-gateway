# gateway/app/chunker.py
"""
Разбиение длинных текстов на чанки с перекрытием (overlap).
Сохраняет точные индексы символов для последующей сборки.
"""
import logging
from typing import List
from .schemas import TextChunk

logger = logging.getLogger("gateway.chunker")

def chunk_text(
    text: str, 
    max_chars: int = 3000, 
    overlap_chars: int = 250
) -> List[TextChunk]:
    """
    Разбивает текст на чанки.
    
    :param text: Исходный полный текст документа.
    :param max_chars: Максимальная длина одного чанка.
    :param overlap_chars: Размер перекрытия между соседними чанками. 
                          Должен быть больше максимальной длины PII-сущности.
    :return: Список объектов TextChunk с привязкой к оригинальным индексам.
    """
    if not text:
        return []

    if len(text) <= max_chars:
        return [TextChunk(text=text, start_idx=0, end_idx=len(text))]

    chunks = []
    start_idx = 0
    text_len = len(text)

    while start_idx < text_len:
        end_idx = min(start_idx + max_chars, text_len)
        
        # Извлекаем кусок текста
        chunk_text_content = text[start_idx:end_idx]
        chunks.append(TextChunk(
            text=chunk_text_content,
            start_idx=start_idx,
            end_idx=end_idx
        ))
        
        # Если дошли до конца, прерываем цикл
        if end_idx == text_len:
            break
            
        # Сдвигаем стартовый индекс с учетом перекрытия
        start_idx += (max_chars - overlap_chars)

    logger.debug(
        "Текст длиной %d символов разбит на %d чанков (max_chars=%d, overlap=%d)",
        text_len, len(chunks), max_chars, overlap_chars
    )
    
    return chunks