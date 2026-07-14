"""
Хирургическая анонимизация текста по координатам:
- принимает исходный текст
- принимает список сущностей (глобальные start/end) от Filter Service
- решает дубликаты и перекрытия (overlap из чанков)
- заменяет на маркеры вида [PERSON], [INN], ...

Принципы безопасности:
- никогда не заменяем, если span некорректный или не совпадает с текстом
- замены делаем с конца (reverse order), чтобы не ломать индексы
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, List, Tuple

logger = logging.getLogger("gateway.anonymizer")


@dataclass(frozen=True)
class EntitySpan:
    start: int
    end: int
    text: str
    type: str  # ожидаем значения типа "PERSON", "INN", ...


# Приоритет типов, если два span пересекаются/конфликтуют
_TYPE_PRIORITY = {
    "PASSPORT": 0,
    "SNILS": 1,
    "INN": 2,
    "DATE_OF_BIRTH": 3,
    "EMAIL": 4,
    "PHONE": 5,
    "ADDRESS": 6,
    "PERSON": 7,
    "OTHER": 8,
}


def anonymize_text(text: str, entities: Iterable[dict[str, Any] | EntitySpan]) -> Tuple[str, List[EntitySpan]]:
    """
    Возвращает:
    - anonymized_text
    - applied_entities: список реально применённых сущностей (после дедупа/разрешения конфликтов)
    """
    normalized = _normalize_and_validate(text, entities)
    if not normalized:
        return text, []

    resolved = _dedupe_and_resolve_overlaps(normalized)
    anonymized = _apply_replacements(text, resolved)
    return anonymized, resolved


def _normalize_and_validate(
    text: str,
    entities: Iterable[dict[str, Any] | EntitySpan],
) -> List[EntitySpan]:
    out: List[EntitySpan] = []
    n = len(text)

    for item in entities:
        if isinstance(item, EntitySpan):
            e = item
        else:
            try:
                e = EntitySpan(
                    start=int(item["start"]),
                    end=int(item["end"]),
                    text=str(item["text"]),
                    type=str(item["type"]).strip().upper(),
                )
            except Exception:  # noqa: BLE001
                logger.warning("Drop entity: invalid structure: %r", item)
                continue

        if e.start < 0 or e.end < 0 or e.start >= e.end or e.end > n:
            logger.warning("Drop entity: out of range span (%d,%d) len=%d", e.start, e.end, n)
            continue

        # ВАЖНО: строгая проверка совпадения, иначе можно повредить документ
        if text[e.start : e.end] != e.text:
            logger.warning(
                "Drop entity: span text mismatch at (%d,%d). expected=%r actual=%r",
                e.start, e.end, e.text, text[e.start : e.end],
            )
            continue

        out.append(e)

    return out


def _dedupe_and_resolve_overlaps(entities: List[EntitySpan]) -> List[EntitySpan]:
    """
    1) Убираем точные дубликаты (start,end,type,text).
    2) Разрешаем overlap-конфликты:
       - предпочитаем более длинный span,
       - при равной длине — более приоритетный type,
       - при равенстве — более ранний.
    """
    if not entities:
        return []

    # unique exact
    entities = list({e for e in entities})

    def prio(e: EntitySpan) -> int:
        return _TYPE_PRIORITY.get(e.type, 99)

    # сортируем для "жадного" выбора
    # start asc, length desc, type priority asc
    entities.sort(key=lambda e: (e.start, -(e.end - e.start), prio(e)))

    selected: List[EntitySpan] = []

    for e in entities:
        if not selected:
            selected.append(e)
            continue

        last = selected[-1]
        if e.start >= last.end:
            selected.append(e)
            continue

        # overlap
        last_len = last.end - last.start
        e_len = e.end - e.start

        if e_len > last_len:
            selected[-1] = e
        elif e_len == last_len:
            if prio(e) < prio(last):
                selected[-1] = e
            # иначе оставляем last
        # если короче — игнорируем

    # финально сортируем по start asc (удобно для отладки)
    selected.sort(key=lambda x: x.start)
    return selected


def _apply_replacements(text: str, entities: List[EntitySpan]) -> str:
    """
    Применяет замены с конца, чтобы индексы не съезжали.
    """
    if not entities:
        return text

    # Чтобы избежать проблем с перекрытиями (если остались) — заменяем с конца.
    entities_desc = sorted(entities, key=lambda e: e.start, reverse=True)

    out = text
    for e in entities_desc:
        marker = _marker_for_type(e.type)
        out = out[: e.start] + marker + out[e.end :]

    return out


def _marker_for_type(pii_type: str) -> str:
    """
    Можно сделать маппинг на более "человеческие" маркеры.
    Сейчас сохраняем тип как есть: [PERSON], [INN], ...
    """
    t = (pii_type or "PII").strip().upper()
    # На всякий случай ограничим "плохие" типы
    if not t.isidentifier() and t not in _TYPE_PRIORITY:
        t = "PII"
    return f"[{t}]"