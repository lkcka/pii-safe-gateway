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

_SELF_TEST_TEXT_HINT_COORDINATES = (
    "Заявку подал Иванов Иван Иванович, тел. +7 921 555-12-34, email ivanov@example.com."
)
_SELF_TEST_HINTS_COORDINATES = [
    Hint(text="+7 921 555-12-34", start=40, end=56, suggested_type="phone"),
    Hint(text="ivanov@example.com", start=64, end=82, suggested_type="email"),
]

# Maps gateway regex suggested_type to entity types that can legitimately match that hint.
_SUGGESTED_TYPE_ENTITY_TYPES: dict[str, frozenset[PIIType]] = {
    "email": frozenset({PIIType.EMAIL}),
    "phone": frozenset({PIIType.PHONE}),
    "date": frozenset({PIIType.DATE_OF_BIRTH, PIIType.NOT_PII}),
    "12_digit_number": frozenset({
        PIIType.INN,
        PIIType.PASSPORT,
        PIIType.SNILS,
        PIIType.NOT_PII,
        PIIType.OTHER,
    }),
}


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
            "combined-regression + hint-coordinates): промпт и парсинг работают корректно."
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

        try:
            hints_coords_result = self.extract(
                _SELF_TEST_TEXT_HINT_COORDINATES, hints=_SELF_TEST_HINTS_COORDINATES
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Self-test (hint-coordinates) не пройден: {exc}") from exc

        for hint, expected_type in (
            (_SELF_TEST_HINTS_COORDINATES[0], PIIType.PHONE),
            (_SELF_TEST_HINTS_COORDINATES[1], PIIType.EMAIL),
        ):
            if _SELF_TEST_TEXT_HINT_COORDINATES[hint.start:hint.end] != hint.text:
                raise RuntimeError(
                    "Self-test (hint-coordinates): некорректные координаты hint "
                    f"({hint.start},{hint.end}) для '{hint.text}'"
                )
            matched = [
                e for e in hints_coords_result.entities
                if e.type == expected_type
                and e.start == hint.start
                and e.end == hint.end
                and e.text == hint.text
            ]
            if not matched:
                raise RuntimeError(
                    "Self-test (hint-coordinates) не пройден: "
                    f"ожидалась сущность {expected_type.value} с координатами "
                    f"({hint.start},{hint.end}) для '{hint.text}', "
                    f"получено: {hints_coords_result.entities}"
                )

        if not any(e.type == PIIType.PERSON for e in hints_coords_result.entities):
            logger.warning(
                "Self-test (hint-coordinates): PERSON не найден среди %s — "
                "не фатально, но желательно стабильное обнаружение ФИО вне hints",
                hints_coords_result.entities,
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
        resolved_hint_indices: set[int] = set()
        for item in parsed.get("entities", []):
            entity = _parse_entity(text, item, raw_content, hints, resolved_hint_indices)
            if entity is not None:
                validated.append(entity)

        if hints and len(resolved_hint_indices) < len(hints):
            missing = [
                hints[i].text for i in range(len(hints)) if i not in resolved_hint_indices
            ]
            logger.warning(
                "Hint coverage incomplete: resolved %d/%d hints; missing decisions for: %s",
                len(resolved_hint_indices),
                len(hints),
                missing,
            )

        return ExtractPIIResponse(entities=validated)


def _parse_json_leniently(raw_content: str) -> dict:
    """
    Пытается разобрать ответ модели как JSON. При обычной неудаче (лишние
    символы, markdown-обёртка, висячие запятые) применяет json_repair —
    библиотеку, специально предназначенную для восстановления "почти
    валидного" JSON, который часто генерируют LLM без constrained decoding.
    """
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError:
        logger.warning(
            "Прямой json.loads не удался, пробуем json_repair. Сырой ответ: %s",
            raw_content,
        )
        try:
            parsed = json_repair.loads(raw_content)
        except Exception as exc:  # noqa: BLE001
            logger.error("json_repair тоже не справился с ответом: %s", raw_content)
            raise ValueError("Не удалось разобрать JSON-ответ локальной LLM даже с repair") from exc

    # Нормализация: допускаем, что модель вернула
    # 1) нормальный dict {"entities": [...]}
    # 2) list [entity, entity, ...]
    # 3) single-entity dict {"start":..,"end":..,"text":..,"type":..}
    if isinstance(parsed, dict):
        if "entities" in parsed and isinstance(parsed["entities"], list):
            return parsed

        # single entity object -> wrap
        if {"start", "end", "text", "type"}.issubset(parsed.keys()):
            return {"entities": [parsed]}

        logger.error("JSON dict неожиданной формы: %r (raw=%s)", parsed, raw_content)
        raise ValueError("JSON-ответ неожиданной формы (dict без entities)")

    if isinstance(parsed, list):
        # модель вернула список сущностей без корневого объекта
        return {"entities": parsed}

    logger.error("json_repair вернул неожиданный тип: %r (raw=%s)", type(parsed), raw_content)
    raise ValueError("json_repair вернул структуру неожиданного типа")


def _hint_type_matches_entity(hint: Hint, entity_type: PIIType) -> bool:
    """True when entity type is compatible with the regex suggested_type of a hint."""
    allowed = _SUGGESTED_TYPE_ENTITY_TYPES.get(hint.suggested_type.lower())
    if allowed is None:
        return True
    return entity_type in allowed


def _find_matching_hint(
    hints: list[Hint],
    claimed_text: str,
    entity_type: PIIType,
    start: Optional[int] = None,
    end: Optional[int] = None,
) -> Optional[tuple[int, Hint]]:
    """
    Match an entity to a hint by text. Returns (hint_index, hint) or None.
    When multiple hints share the same text, disambiguate by suggested_type/type,
    then by exact coordinates; otherwise drop (return None).
    """
    candidates: list[tuple[int, Hint]] = [
        (i, h) for i, h in enumerate(hints) if h.text == claimed_text
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    typed = [(i, h) for i, h in candidates if _hint_type_matches_entity(h, entity_type)]
    if len(typed) == 1:
        return typed[0]

    if start is not None and end is not None:
        coord = [(i, h) for i, h in candidates if h.start == start and h.end == end]
        if len(coord) == 1:
            return coord[0]

    logger.warning(
        "Неоднозначное сопоставление hint для '%s' (type=%s): %d кандидатов",
        claimed_text,
        entity_type,
        len(candidates),
    )
    return None


def _recover_coordinates_from_hints(
    hints: list[Hint],
    claimed_text: str,
    entity_type: PIIType,
) -> Optional[tuple[int, int, int]]:
    """
    When the model omitted start/end, recover coordinates from a matching hint.
    Returns (hint_index, start, end) or None.
    """
    match = _find_matching_hint(hints, claimed_text, entity_type)
    if match is None:
        return None
    hint_index, hint = match
    return hint_index, hint.start, hint.end


def _parse_entity(
    text: str,
    item: object,
    raw_content: str,
    hints: list[Hint],
    resolved_hint_indices: set[int],
) -> Optional[PIIEntity]:
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

    if "text" not in item or "type" not in item:
        logger.warning(
            "Отброшена сущность: отсутствуют обязательные поля text/type. Получено: %s",
            item,
        )
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

    has_coords = "start" in item and "end" in item
    hint_index: Optional[int] = None

    if has_coords:
        try:
            start = int(item["start"])
            end = int(item["end"])
        except (TypeError, ValueError):
            logger.warning("Отброшена сущность: нечисловые start/end: %s", item)
            return None
    elif hints:
        recovered = _recover_coordinates_from_hints(hints, claimed_text, entity_type)
        if recovered is None:
            logger.warning(
                "Отброшена сущность: отсутствуют start/end, не удалось восстановить из hints: %s",
                item,
            )
            return None
        hint_index, start, end = recovered
        logger.info(
            "Восстановлены координаты из hint для '%s': (%d,%d)",
            claimed_text, start, end,
        )
    else:
        logger.warning(
            "Отброшена сущность: отсутствуют start/end и hints пуст: %s",
            item,
        )
        return None

    if hint_index is None:
        match = _find_matching_hint(hints, claimed_text, entity_type, start, end)
        if match is not None:
            hint_index, _ = match

    if hint_index is not None:
        resolved_hint_indices.add(hint_index)

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