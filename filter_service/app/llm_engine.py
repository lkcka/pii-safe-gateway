"""
Обёртка над llama-cpp-python: ленивая загрузка модели (singleton на процесс),
генерация JSON (опционально constrained по GBNF), мягкий разбор с repair
и защита от галлюцинаций координат в ответе модели.
"""
import json
import logging
import os
import threading
from typing import Optional

import json_repair
from llama_cpp import Llama, LlamaGrammar

from .config import settings
from .grammar import build_pii_grammar
from .prompts import SYSTEM_PROMPT, build_user_prompt
from .schemas import ExtractPIIResponse, Hint, PIIEntity, PIIType

logger = logging.getLogger("filter_service.llm_engine")

_lock = threading.Lock()
_engine: Optional["FilterEngine"] = None

# Минимально необходимые ключи в entity-объекте. "reason" опционален —
# без grammar модель иногда может его пропустить, это не повод отбрасывать
# всю сущность (в отличие от отсутствия start/end/text/type).
_MINIMAL_REQUIRED_KEYS = frozenset({"start", "end", "text", "type"})

_SELF_TEST_TEXT_NEGATIVE = (
    "Настоящим уведомляем о переносе срока поставки на следующий квартал."
)

_SELF_TEST_TEXT_POSITIVE = (
    "Клиент Иванов Иван Иванович, телефон +7 921 555-12-34."
)

_SELF_TEST_TEXT_HINT_REJECTION = (
    "Номер договора 5062024891234567890 от 01.02.2024."
)
_SELF_TEST_HINTS_REJECTION = [
    Hint(text="5062024891234567890", start=15, end=35, suggested_type="12_digit_number")
]

_SELF_TEST_TEXT_COMBINED = (
    "Номер договора 5062024891234567890 от 01.02.2024. "
    "Исполнитель: Сидоров А.В., ИНН 500100732259."
)
_SELF_TEST_HINTS_COMBINED = [
    Hint(text="5062024891234567890", start=15, end=35, suggested_type="12_digit_number"),
    Hint(text="500100732259", start=83, end=95, suggested_type="12_digit_number"),
]


class FilterEngine:
    """Singleton-обёртка над Llama: одна загруженная модель на процесс."""

    def __init__(self) -> None:
        if not os.path.isfile(settings.filter_model_path):
            raise FileNotFoundError(
                f"Файл модели не найден: {settings.filter_model_path}. "
                "Проверьте FILTER_MODEL_PATH и наличие .gguf в volume ./models."
            )

        logger.info(
            "Загрузка модели %s (ctx=%d, threads=%d, batch=%d, n_gpu_layers=%d, grammar=%s)",
            settings.filter_model_path,
            settings.filter_model_ctx,
            settings.filter_model_threads,
            settings.filter_model_batch,
            settings.n_gpu_layers,
            settings.filter_use_grammar,
        )

        self._llm = Llama(
            model_path=settings.filter_model_path,
            n_ctx=settings.filter_model_ctx,
            n_threads=settings.filter_model_threads,
            n_batch=settings.filter_model_batch,
            n_gpu_layers=settings.n_gpu_layers,
            chat_format="chatml",
            verbose=settings.filter_model_verbose,
        )

        # Grammar компилируется, только если реально используется — компиляция
        # не бесплатна, и незачем платить за неё, если FILTER_USE_GRAMMAR=false.
        self._grammar: Optional[LlamaGrammar] = (
            build_pii_grammar() if settings.filter_use_grammar else None
        )
        logger.info("Модель загружена.")

        self._self_test()
        logger.info(
            "Self-test пройден (негативный + позитивный + hint-rejection + "
            "combined-regression): промпт и парсинг работают корректно."
        )

    def _self_test(self) -> None:
        """
        Функциональные проверки при старте (НЕ завязаны на точную схему JSON —
        только на итоговое семантическое поведение engine.extract()). Любое
        несоответствие — фатальная ошибка старта процесса, не тихая деградация.
        """
        try:
            negative_result = self.extract(_SELF_TEST_TEXT_NEGATIVE, hints=[])
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Self-test (негативный) не пройден: {exc}") from exc

        if negative_result.entities:
            raise RuntimeError(
                f"Self-test (негативный) не пройден: найдены сущности {negative_result.entities}"
            )

        try:
            positive_result = self.extract(_SELF_TEST_TEXT_POSITIVE, hints=[])
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Self-test (позитивный) не пройден: {exc}") from exc

        if not any(e.type == PIIType.PERSON for e in positive_result.entities):
            raise RuntimeError(
                f"Self-test (позитивный) не пройден: PERSON не найден среди {positive_result.entities}"
            )

        try:
            rejection_result = self.extract(
                _SELF_TEST_TEXT_HINT_REJECTION, hints=_SELF_TEST_HINTS_REJECTION
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Self-test (hint-rejection) не пройден: {exc}") from exc

        if rejection_result.entities:
            raise RuntimeError(
                "Self-test (hint-rejection) не пройден: номер договора должен "
                f"быть отклонён, но получено: {rejection_result.entities}"
            )

        try:
            combined_result = self.extract(
                _SELF_TEST_TEXT_COMBINED, hints=_SELF_TEST_HINTS_COMBINED
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Self-test (combined-regression) не пройден: {exc}") from exc

        if len(combined_result.entities) != 1 or combined_result.entities[0].type != PIIType.INN:
            raise RuntimeError(
                "Self-test (combined-regression) не пройден: ожидался ровно один "
                f"ИНН и полное отклонение номера договора, получено: {combined_result.entities}."
            )

    def extract(self, text: str, hints: list[Hint]) -> ExtractPIIResponse:
        """
        Прогоняет текст через локальную LLM. JSON парсится мягко: сначала
        обычный json.loads, при неудаче — json_repair (устраняет типичные
        огрехи LLM-вывода: висячие запятые, markdown-обёртку, недоэкранированные
        кавычки). Сущности с вердиктом NOT_PII отфильтровываются. Сущности с
        некорректной структурой/типом отбрасываются ПОШТУЧНО с логом, не ломая
        весь запрос — это компенсирует отсутствие жёсткой grammar-гарантии.
        """
        user_prompt = build_user_prompt(text, hints)

        completion = self._llm.create_chat_completion(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            grammar=self._grammar,  # None, если FILTER_USE_GRAMMAR=false
            temperature=0.0,
            top_p=1.0,
            max_tokens=settings.filter_model_max_tokens,
            stop=["```"],  # доп. страховка от markdown-обёртки без grammar
        )

        raw_content = completion["choices"][0]["message"]["content"]
        parsed = _parse_json_leniently(raw_content)

        validated: list[PIIEntity] = []
        for item in parsed.get("entities", []):
            entity = _parse_entity(text, item, raw_content)
            if entity is not None:
                validated.append(entity)

        return ExtractPIIResponse(entities=validated)


def _parse_json_leniently(raw_content: str) -> dict:
    """
    Пытается разобрать ответ модели как JSON. При обычной неудаче (лишние
    символы, markdown-обёртка, висячие запятые) применяет json_repair —
    библиотеку, специально предназначенную для восстановления "почти
    валидного" JSON, который часто генерируют LLM без constrained decoding.
    """
    try:
        return json.loads(raw_content)
    except json.JSONDecodeError:
        logger.warning(
            "Прямой json.loads не удался, пробуем json_repair. Сырой ответ: %s",
            raw_content,
        )

    try:
        repaired = json_repair.loads(raw_content)
    except Exception as exc:  # noqa: BLE001
        logger.error("json_repair тоже не справился с ответом: %s", raw_content)
        raise ValueError(
            "Не удалось разобрать JSON-ответ локальной LLM даже с repair"
        ) from exc

    if not isinstance(repaired, dict):
        logger.error("json_repair вернул не словарь: %r (ответ: %s)", repaired, raw_content)
        raise ValueError("json_repair вернул структуру неожиданного типа")

    return repaired


def _parse_entity(text: str, item: object, raw_content: str) -> Optional[PIIEntity]:
    """
    Мягкая валидация одной сущности. В отличие от прежней жёсткой проверки
    (точное совпадение множества ключей), здесь достаточно наличия минимально
    необходимых полей — "reason" опционален. Некорректные по структуре или
    типу сущности отбрасываются ПОШТУЧНО (с warning), не прерывая обработку
    остальных сущностей в чанке.
    """
    if not isinstance(item, dict):
        logger.warning("Отброшена сущность: не является объектом: %r", item)
        return None

    if not _MINIMAL_REQUIRED_KEYS.issubset(item.keys()):
        logger.warning(
            "Отброшена сущность: отсутствуют обязательные поля %s. Получено: %s",
            _MINIMAL_REQUIRED_KEYS - item.keys(), item,
        )
        return None

    try:
        start = int(item["start"])
        end = int(item["end"])
    except (TypeError, ValueError):
        logger.warning("Отброшена сущность: нечисловые start/end: %s", item)
        return None

    claimed_text = str(item["text"])
    reason = str(item.get("reason", ""))

    raw_type = str(item["type"]).strip().upper()
    if raw_type not in PIIType.__members__:
        logger.warning(
            "Отброшена сущность: неизвестный тип '%s' (полный ответ: %s)",
            raw_type, raw_content,
        )
        return None

    entity_type = PIIType[raw_type]
    if entity_type == PIIType.NOT_PII:
        logger.debug("Кандидат '%s' явно отклонён моделью (NOT_PII)", claimed_text)
        return None

    return _repair_coordinates(text, start, end, claimed_text, entity_type, reason)


def _repair_coordinates(
    text: str, start: int, end: int, claimed_text: str, entity_type: PIIType, reason: str
) -> Optional[PIIEntity]:
    """
    Защита от галлюцинаций координат: если text[start:end] не совпадает
    с заявленным "text", ищем точное вхождение рядом с указанной позицией,
    затем — во всём тексте чанка. Если не найдено — сущность отбрасывается:
    лучше пропустить кандидата, чем повредить документ заменой не того фрагмента.
    """
    if 0 <= start < end <= len(text) and text[start:end] == claimed_text:
        return PIIEntity(start=start, end=end, text=claimed_text, type=entity_type, reason=reason)

    search_start = max(0, start - 50)
    search_end = min(len(text), end + 50)
    window = text[search_start:search_end]

    idx = window.find(claimed_text)
    if idx != -1:
        real_start = search_start + idx
        real_end = real_start + len(claimed_text)
        logger.info(
            "Скорректированы координаты '%s': (%d,%d) -> (%d,%d)",
            claimed_text, start, end, real_start, real_end,
        )
        return PIIEntity(start=real_start, end=real_end, text=claimed_text, type=entity_type, reason=reason)

    idx = text.find(claimed_text)
    if idx != -1:
        return PIIEntity(start=idx, end=idx + len(claimed_text), text=claimed_text, type=entity_type, reason=reason)

    logger.warning(
        "Сущность '%s' (тип %s) отброшена: точное вхождение не найдено в чанке",
        claimed_text, entity_type,
    )
    return None


def get_engine() -> FilterEngine:
    """Лениво инициализирует и кеширует единственный экземпляр FilterEngine."""
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                _engine = FilterEngine()
    return _engine