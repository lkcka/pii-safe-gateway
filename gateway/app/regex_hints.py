"""
Быстрый regex-поиск кандидатов (hints) для Filter Service.

ВАЖНО:
- Regex тут НЕ принимает финального решения "это PII" — только собирает кандидатов.
- suggested_type — свободная строка. Для совместимости с текущим промптом filter-service
  используем значения: email | phone | date | 12_digit_number (как "числовой кандидат").
"""
from __future__ import annotations

import re
from typing import TypedDict, List, Tuple


class HintDict(TypedDict):
    text: str
    start: int
    end: int
    suggested_type: str


# --- Patterns ---
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)

# Достаточно строгий паттерн под РФ-форматы +7/8 XXX XXX-XX-XX
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+7|8)\s*(?:\(?\d{3}\)?[\s\-]*)\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)"
)

# Даты: dd.mm.yyyy / dd-mm-yyyy / dd/mm/yyyy и упрощённо dd.mm.yy
_DATE_RE = re.compile(
    r"(?<!\d)(?:0?[1-9]|[12]\d|3[01])[.\-/](?:0?[1-9]|1[0-2])[.\-/](?:\d{2}|\d{4})(?!\d)"
)

# "Числовые" кандидаты (ИНН/СНИЛС/паспорт/номер договора и т.п.) — как hints.
# Ловим непрерывные группы >=10 цифр (без пробелов).
_LONG_DIGITS_RE = re.compile(r"(?<!\d)\d{10,25}(?!\d)")

# Ловим также "разделённые" варианты, чтобы подхватить паспорт вида "4510 123456"
# или СНИЛС с пробелами/дефисами (частично). Затем отфильтруем по числу цифр.
_SPLIT_DIGITS_RE = re.compile(r"(?<!\d)(?:\d[\s\-]?){10,30}\d(?!\d)")


def find_hints(text: str) -> List[HintDict]:
    """
    Возвращает список hints (кандидатов) с координатами в переданном `text`.
    Гарантирует:
    - стабильный порядок (по start),
    - отсутствие перекрывающихся hints (например, число внутри телефона не вернётся отдельно).
    """
    matches: List[Tuple[int, int, str]] = []

    matches += _collect(_EMAIL_RE, text, "email")
    matches += _collect(_PHONE_RE, text, "phone")
    matches += _collect(_DATE_RE, text, "date")

    # Числа: сначала "чистые" длинные, потом "разделённые" (passport/snils-like).
    matches += _collect(_LONG_DIGITS_RE, text, "12_digit_number")
    matches += _collect_split_digits(text)

    # Убираем перекрытия: приоритет email > phone > date > numbers
    prioritized = _dedupe_and_remove_overlaps(matches)

    return [
        HintDict(text=text[s:e], start=s, end=e, suggested_type=t)
        for s, e, t in prioritized
    ]


def _collect(pattern: re.Pattern[str], text: str, suggested_type: str) -> List[Tuple[int, int, str]]:
    out: List[Tuple[int, int, str]] = []
    for m in pattern.finditer(text):
        s, e = m.span()
        if s < e:
            out.append((s, e, suggested_type))
    return out


def _collect_split_digits(text: str) -> List[Tuple[int, int, str]]:
    out: List[Tuple[int, int, str]] = []
    for m in _SPLIT_DIGITS_RE.finditer(text):
        s, e = m.span()
        if s >= e:
            continue
        chunk = text[s:e]
        digits_only = re.sub(r"\D", "", chunk)
        # Берём только действительно "длинные" штуки
        if len(digits_only) >= 10:
            out.append((s, e, "12_digit_number"))
    return out


def _dedupe_and_remove_overlaps(matches: List[Tuple[int, int, str]]) -> List[Tuple[int, int, str]]:
    """
    Удаляет:
    - точные дубликаты,
    - перекрывающиеся интервалы (оставляем более приоритетный).
    """
    if not matches:
        return []

    priority = {"email": 0, "phone": 1, "date": 2, "12_digit_number": 3}

    # 1) уникализируем точные дубли
    uniq = list({(s, e, t) for (s, e, t) in matches})

    # 2) сортируем: start asc, priority asc, length desc (чтобы более "крупный" матч побеждал)
    uniq.sort(key=lambda x: (x[0], priority.get(x[2], 99), -(x[1] - x[0])))

    selected: List[Tuple[int, int, str]] = []
    for s, e, t in uniq:
        if not selected:
            selected.append((s, e, t))
            continue

        ps, pe, pt = selected[-1]
        if s >= pe:
            selected.append((s, e, t))
            continue

        # Есть overlap с последним выбранным
        curr_pr = priority.get(t, 99)
        prev_pr = priority.get(pt, 99)

        if curr_pr < prev_pr:
            # текущий более приоритетный — заменяем предыдущий
            selected[-1] = (s, e, t)
        elif curr_pr == prev_pr:
            # одинаковый класс: оставляем более длинный
            if (e - s) > (pe - ps):
                selected[-1] = (s, e, t)
            # иначе игнорируем
        # если текущий менее приоритетный — игнорируем

    # финальная сортировка по start (для стабильности)
    selected.sort(key=lambda x: x[0])
    return selected