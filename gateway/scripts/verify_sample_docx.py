#!/usr/bin/env python3
"""
Verify DOCX extraction + PII anonymization pipeline without external LLM.
Exit 0 when applied >= MinApplied (default 2).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import httpx

# gateway/app on sys.path when run as: python scripts/verify_sample_docx.py
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.anonymizer import anonymize_text
from app.chunker import chunk_text
from app.extractors import extract_text
from app.regex_hints import find_hints

MIN_APPLIED = int(os.environ.get("SMOKE_MIN_APPLIED", "2"))
FILTER_URL = os.environ.get("FILTER_SERVICE_URL", "http://localhost:8001").rstrip("/")
CHUNK_MAX = int(os.environ.get("CHUNK_MAX_CHARS", "3000"))
CHUNK_OVERLAP = int(os.environ.get("CHUNK_OVERLAP_CHARS", "250"))
FILTER_TIMEOUT = float(os.environ.get("FILTER_REQUEST_TIMEOUT", "120"))
_MARKER_RE = re.compile(r"\[[A-Z_]+\]")


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    b64_path = script_dir / "sample.docx.b64.txt"
    if not b64_path.exists():
        print("ERROR: sample.docx.b64.txt not found. Run: python scripts/create_sample_docx.py", file=sys.stderr)
        return 1

    b64 = b64_path.read_text(encoding="ascii").strip()
    text = extract_text("sample.docx", b64)

    print("=== Extracted text ===")
    print(text)
    print()

    # Structural checks (gateway extractors)
    required_parts = ["Иванов Иван Иванович", "Телефон", "+7 921 555-12-34", "ivanov@example.com"]
    missing = [p for p in required_parts if p not in text]
    if missing:
        print(f"FAIL: extracted text missing: {missing}", file=sys.stderr)
        return 1

    order_ok = (
        text.index("Иванов") < text.index("Телефон") < text.index("уполномоченным")
    )
    if not order_ok:
        print("FAIL: block order is wrong (paragraph/table/paragraph)", file=sys.stderr)
        return 1

    chunks = chunk_text(text, max_chars=CHUNK_MAX, overlap_chars=CHUNK_OVERLAP)
    global_entities: list[dict[str, object]] = []

    try:
        with httpx.Client(timeout=FILTER_TIMEOUT) as client:
            for ch in chunks:
                hints = find_hints(ch.text)
                resp = client.post(
                    f"{FILTER_URL}/extract-pii",
                    json={"text": ch.text, "hints": hints},
                )
                resp.raise_for_status()
                data = resp.json()
                for e in data.get("entities", []):
                    try:
                        global_entities.append(
                            {
                                "start": ch.start_idx + int(e["start"]),
                                "end": ch.start_idx + int(e["end"]),
                                "text": str(e["text"]),
                                "type": str(e["type"]).strip().upper(),
                            }
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
    except httpx.HTTPError as exc:
        print(f"FAIL: Filter Service unreachable at {FILTER_URL}: {exc}", file=sys.stderr)
        return 1

    anonymized, applied = anonymize_text(text, global_entities)

    print("=== Anonymized text ===")
    print(anonymized)
    print()
    print(f"entities={len(global_entities)} applied={len(applied)}")

    found = sorted(set(_MARKER_RE.findall(anonymized)))
    print(f"markers in anonymized text: {', '.join(found) if found else '(none)'}")

    if len(applied) >= MIN_APPLIED:
        print(f"OK: applied >= {MIN_APPLIED}")
        return 0

    print(
        f"FAIL: applied={len(applied)} < {MIN_APPLIED}. "
        "Filter Service may be non-deterministic on CPU — retry once.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
