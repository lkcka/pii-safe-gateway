"""
Быстрый regex-поиск кандидатов (hints) для Filter Service.

ВАЖНО:
- Regex тут НЕ принимает финального решения "это PII" — только собирает кандидатов.
- suggested_type — свободная строка. Для совместимости с текущим промптом filter-service
  используем значения: email | phone | date | 12_digit_number | person_name.
"""
from __future__ import annotations

import re
from typing import TypedDict, List, Tuple

class HintDict(TypedDict):
    text: str
    start: int
    end: int
    suggested_type: str


# Cyrillic token: capital letter + lowercase (optional hyphenated part).
_CYR_WORD = r"[А-ЯЁ][а-яё]+(?:\-[А-ЯЁ][а-яё]+)?"

# Typical Russian surname endings (reduces false positives like "Генеральный Директор").
_CYR_SURNAME = (
    r"[А-ЯЁ][а-яё]*(?:"
    r"ов|ев|ёв|ин|ын|ий|ой|ая|яя|ко|ук|юк|ец|"
    r"ова|ева|ёва|ина|ына|"
    r"ский|ская|цкий|цкая|енко|швили"
    r")"
)

# Patronymic suffix on the third token in full FIO.
_PATRONYMIC_SUFFIX = r"(?:ович|евич|овна|евна|ична|ьевич|ьевна)"

# Word boundary: not preceded by Cyrillic letter (avoids partial matches inside words).
_NOT_IN_WORD_L = r"(?<![А-ЯЁа-яё])"
_NOT_IN_WORD_R = r"(?![а-яё])"

_FIO_FULL = (
    rf"{_NOT_IN_WORD_L}{_CYR_SURNAME}\s+{_CYR_WORD}\s+{_CYR_WORD}{_PATRONYMIC_SUFFIX}{_NOT_IN_WORD_R}"
)
_FIO_INITIALS = (
    rf"{_NOT_IN_WORD_L}{_CYR_SURNAME}\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.?"
)
_FIO_SHORT = (
    rf"{_NOT_IN_WORD_L}{_CYR_SURNAME}\s+{_CYR_WORD}{_NOT_IN_WORD_R}"
    rf"(?!\s+{_CYR_WORD})"
)

_PERSON_NAME_RE = re.compile(
    rf"(?:{_FIO_FULL}|{_FIO_INITIALS}|{_FIO_SHORT})",
    re.UNICODE,
)

_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)

_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+7|8)\s*(?:\(?\d{3}\)?[\s\-]*)\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)"
)

_DATE_RE = re.compile(
    r"(?<!\d)(?:0?[1-9]|[12]\d|3[01])[.\-/](?:0?[1-9]|1[0-2])[.\-/](?:\d{2}|\d{4})(?!\d)"
)

_LONG_DIGITS_RE = re.compile(r"(?<!\d)\d{10,25}(?!\d)")
_SPLIT_DIGITS_RE = re.compile(r"(?<!\d)(?:\d[\s\-]?){10,30}\d(?!\d)")

PATTERNS: dict[str, re.Pattern[str]] = {
    "email": _EMAIL_RE,
    "phone": _PHONE_RE,
    "date": _DATE_RE,
    "person_name": _PERSON_NAME_RE,
    "12_digit_number": _LONG_DIGITS_RE,
}


def find_hints(text: str) -> List[HintDict]:
    """
    Возвращает список hints (кандидатов) с координатами в переданном `text`.
    Гарантирует:
    - стабильный порядок (по start),
    - отсутствие перекрывающихся hints (например, число внутри телефона не вернётся отдельно).
    """
    matches: List[Tuple[int, int, str]] = []

    matches += _collect(PATTERNS["email"], text, "email")
    matches += _collect(PATTERNS["phone"], text, "phone")
    matches += _collect(PATTERNS["person_name"], text, "person_name")
    matches += _collect(PATTERNS["date"], text, "date")
    matches += _collect(PATTERNS["12_digit_number"], text, "12_digit_number")
    matches += _collect_split_digits(text)

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

    priority = {
        "email": 0,
        "phone": 1,
        "person_name": 2,
        "date": 3,
        "12_digit_number": 4,
    }

    uniq = list({(s, e, t) for (s, e, t) in matches})
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

        curr_pr = priority.get(t, 99)
        prev_pr = priority.get(pt, 99)

        if curr_pr < prev_pr:
            selected[-1] = (s, e, t)
        elif curr_pr == prev_pr:
            if (e - s) > (pe - ps):
                selected[-1] = (s, e, t)

    selected.sort(key=lambda x: x[0])
    return selected
