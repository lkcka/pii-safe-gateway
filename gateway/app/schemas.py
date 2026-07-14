# gateway/app/schemas.py
"""
Схемы данных для Gateway.
Расширяем стандартный OpenAI-совместимый запрос, добавляя поле `files`.
"""
from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, Any

class FileAttachment(BaseModel):
    """Файл, переданный в запросе в формате Base64."""
    filename: str = Field(..., description="Имя файла с расширением (например, contract.docx)")
    content_base64: str = Field(..., description="Содержимое файла в кодировке Base64")

class ChatCompletionRequest(BaseModel):
    """
    Запрос к /v1/chat/completions. 
    extra="allow" гарантирует, что любые специфичные для провайдера поля 
    (например, 'seed', 'tools', 'response_format') пройдут транзитом.
    """
    model_config = ConfigDict(extra="allow")
    
    model: str
    messages: list[dict[str, Any]]
    stream: Optional[bool] = False
    files: Optional[list[FileAttachment]] = Field(
        default=None, 
        description="Расширение API: список файлов для предварительной очистки от PII"
    )

class TextChunk(BaseModel):
    """
    Фрагмент текста для отправки в Filter Service.
    Хранит оригинальные индексы для точной обратной сборки документа.
    """
    text: str
    start_idx: int
    end_idx: int


class GlobalEntity(BaseModel):
    """
    Подтвержденная PII-сущность, привязанная к глобальным координатам 
    всего исходного документа (а не отдельного чанка).
    """
    start: int
    end: int
    text: str
    type: str  # Строковое значение PIIType (например, "PERSON", "INN")