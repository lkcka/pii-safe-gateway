"""Unit tests for Gateway core helpers (regex hints, etc.)."""
from __future__ import annotations

from app.regex_hints import PATTERNS, find_hints


def test_regex_person_name() -> None:
    text = (
        "Заявку подал Иванов Иван Иванович, контакт Иванов И. И., "
        "также Петров Петр. Договор подписан уполномоченным."
    )

    pattern = PATTERNS["person_name"]
    matches = [m.group(0) for m in pattern.finditer(text)]

    assert "Иванов Иван Иванович" in matches
    assert "Иванов И. И." in matches
    assert "Петров Петр" in matches

    assert not any("Договор" in m for m in matches)
    assert not any("подписан" in m for m in matches)

    hints = find_hints(text)
    person_hints = [h for h in hints if h["suggested_type"] == "person_name"]
    person_texts = {h["text"] for h in person_hints}

    assert "Иванов Иван Иванович" in person_texts
    assert "Иванов И. И." in person_texts
    assert "Петров Петр" in person_texts
    assert len(person_hints) == 3


def test_regex_person_name_initials_compact() -> None:
    text = "Подписант: Сидоров И.И."
    matches = [m.group(0) for m in PATTERNS["person_name"].finditer(text)]
    assert matches == ["Сидоров И.И."]
