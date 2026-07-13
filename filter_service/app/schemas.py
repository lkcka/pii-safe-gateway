"""
Pydantic-схемы Filter Service: контракт эндпоинта /extract-pii.
PIIType — единственный источник истины для допустимых типов сущностей,
используется как при валидации FastAPI, так и при генерации GBNF-грамматики
(см. grammar.py), чтобы схема и грамматика никогда не расходились.
"""
from enum import Enum

from pydantic import BaseModel, Field


class PIIType(str, Enum):
    """Типы персональных данных, которые умеет распознавать Filter Service."""

    PERSON = "PERSON"
    PHONE = "PHONE"
    EMAIL = "EMAIL"
    ADDRESS = "ADDRESS"
    DATE_OF_BIRTH = "DATE_OF_BIRTH"
    SNILS = "SNILS"
    INN = "INN"
    PASSPORT = "PASSPORT"
    OTHER = "OTHER"

    # Служебное значение. Используется ТОЛЬКО моделью внутри constrained-
    # декодирования как явный вердикт "этот regex-кандидат НЕ является PII".
    # Заставляет модель явно рассуждать и принимать решение по каждому hint,
    # а не молча пропускать кандидата (что на практике приводило к попыткам
    # модели "пристроить" отклонённый кандидат под тип OTHER, см. историю
    # багфиксов). НИКОГДА не попадает в финальный ответ API — отфильтровывается
    # в FilterEngine.extract() перед формированием ExtractPIIResponse.
    NOT_PII = "NOT_PII"


class Hint(BaseModel):
    """
    Кандидат на PII, найденный Gateway-ом быстрым regex-фильтром
    (email, телефон явного формата, длинные числа, даты).
    Это НЕ финальное решение — только подсказка. LLM обязана сама решить,
    является ли кандидат реальным PII, с учётом контекста абзаца.
    """

    text: str
    start: int
    end: int
    suggested_type: str = Field(
        description="Свободное описание паттерна от regex, например "
                    "'email', 'phone', '12_digit_number', 'date'"
    )


class ExtractPIIRequest(BaseModel):
    text: str
    hints: list[Hint] = Field(default_factory=list)


class PIIEntity(BaseModel):
    start: int
    end: int
    text: str
    type: PIIType
    reason: str = ""


class ExtractPIIResponse(BaseModel):
    entities: list[PIIEntity] = Field(default_factory=list)