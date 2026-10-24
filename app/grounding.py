"""Deterministic checks that tie every model output back to the original document.

- verify_extraction: each item's source_quote must really be in the document, and the
  numbers, frequency and dates the Reader extracted must appear in that quote.
- check_explanation_numbers: the Explainer may rephrase and translate, but every number it
  writes must already be in the verified data for that item.
- reading_grade: Flesch-Kincaid grade, used to show before/after readability.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from difflib import SequenceMatcher

from .models import Appointment, Extraction, Instruction, Medication, WarningGroup

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—−"), "-")
_QUOTES = {ord("‘"): "'", ord("’"): "'", ord("“"): '"', ord("”"): '"'}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").translate(_DASHES).translate(_QUOTES)
    text = re.sub(r"-\s*\n\s*", "-", text)       # words split across lines at a hyphen
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def to_ascii_digits(text: str) -> str:
    """Devanagari, Arabic-Indic and other digits -> 0-9, so number checks work in every language."""
    out = []
    for ch in text or "":
        if ch.isdigit() and not ("0" <= ch <= "9"):
            try:
                out.append(str(unicodedata.digit(ch)))
                continue
            except (TypeError, ValueError):
                pass
        out.append(ch)
    return "".join(out)


_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


def numbers(text: str) -> set[str]:
    """All numbers in text, with thousands separators dropped: '1,000 mg' -> {'1000'}."""
    found = set()
    for m in _NUM_RE.findall(to_ascii_digits(text or "")):
        if re.fullmatch(r"\d{1,3}(,\d{3})+", m):
            m = m.replace(",", "")
        m = m.replace(",", ".")
        if "." in m:
            m = m.rstrip("0").rstrip(".") or "0"
        if m.isdigit():
            m = str(int(m))          # '08' and '8' are the same number
        found.add(m)
    return found


def quote_in_document(quote: str, doc_norm: str, threshold: float = 0.9) -> tuple[bool, float]:
    q = normalize(quote)
    if not q:
        return False, 0.0
    if q in doc_norm:
        return True, 1.0
    # Fuzzy fallback for small extraction differences (line breaks, bullets).
    window = len(q)
    best = 0.0
    step = max(1, window // 8)
    matcher = SequenceMatcher(autojunk=False)
    matcher.set_seq2(q)
    for start in range(0, max(1, len(doc_norm) - window + 1), step):
        chunk = doc_norm[start:start + window + 20]
        matcher.set_seq1(chunk)
        if matcher.real_quick_ratio() < best:
            continue
        r = matcher.ratio()
        if r > best:
            best = r
            if best >= 0.99:
                break
    return best >= threshold, round(best, 3)


_FREQ_WORDS = [
    (re.compile(r"\bonce (a |per )?(daily|day)\b|\bonce\b|\bdaily\b|\bevery (morning|evening|night|day)\b|\bat bedtime\b|\beach morning\b|\b(qd|qam|qpm|qhs|hs)\b"), 1),
    (re.compile(r"\btwice\b|\btwo times\b|\b2 times\b|\bbid\b|\bq12h\b"), 2),
    (re.compile(r"\bthree times\b|\b3 times\b|\btid\b"), 3),
    (re.compile(r"\bfour times\b|\b4 times\b|\bqid\b"), 4),
]


def _stated_frequencies(quote_norm: str) -> set[int]:
    found = set()
    for rx, n in _FREQ_WORDS:
        if rx.search(quote_norm):
            found.add(n)
    if {2, 1} <= found and re.search(r"\btwice\b", quote_norm):
        found.discard(1)   # "twice daily" also matches "daily"
    for n in (3, 4):
        if n in found:
            found.discard(1)
    return found


def _date_variants(iso: str) -> set[str]:
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return {iso}
    return {f"{d.month}/{d.day}", f"{d.month:02d}/{d.day:02d}", f"{d.month}/{d.day}/{d.year}", f"{d.month:02d}/{d.day:02d}/{d.year}"}


def _med_problems(med: Medication, quote_norm: str) -> list[str]:
    problems = []
    quote_nums = numbers(quote_norm)
    for label, value in (("strength", med.strength), ("dose", med.dose), ("max per day", med.max_per_day)):
        missing = numbers(value or "") - quote_nums
        if missing:
            problems.append(f"{label} '{value}' has numbers not in the document text ({', '.join(sorted(missing))})")
    if med.schedule_kind == "fixed" and med.times_per_day:
        stated = _stated_frequencies(quote_norm)
        if med.interval_hours:
            if str(med.interval_hours) not in quote_nums:
                problems.append(f"'every {med.interval_hours} hours' is not in the document text")
        elif stated and med.times_per_day not in stated:
            problems.append(f"{med.times_per_day} times a day does not match the document text")
    if med.duration_days and str(med.duration_days) not in quote_nums:
        problems.append(f"{med.duration_days} days is not in the document text")
    for label, iso in (("end date", med.end_date), ("start date", med.start_date)):
        if iso and not any(v in quote_norm for v in _date_variants(iso)):
            problems.append(f"{label} {iso} is not in the document text")
    for step in med.taper:
        for iso in (step.start_date, step.end_date):
            if not any(v in quote_norm for v in _date_variants(iso)):
                problems.append(f"taper date {iso} is not in the document text")
        missing = numbers(step.dose) - quote_nums
        if missing:
            problems.append(f"taper dose '{step.dose}' has numbers not in the document text")
    return problems


def _to12(hhmm: str) -> str:
    h, m = int(hhmm[:2]), hhmm[3:5]
    return f"{(h % 12) or 12}:{m}"


def _field_problems(label: str, value: str | None, doc_norm: str) -> list[str]:
    """Each sentence of a copied field must be in the document (the Reader may join two sentences)."""
    if not value:
        return []
    parts = [p for p in re.split(r"(?<=[.!?;])\s+", value.strip()) if len(p.strip()) > 3] or [value]
    for part in parts:
        ok, score = quote_in_document(part, doc_norm, threshold=0.85)
        if not ok:
            return [f"{label} '{part}' was not found in the document ({score:.0%})"]
    return []


def verify_extraction(ex: Extraction, doc_text: str) -> dict[str, dict]:
    """Return {item_id: {"verified": bool, "score": float, "problems": [...]}} for every item."""
    doc_norm = normalize(doc_text)
    results: dict[str, dict] = {}
    groups = [ex.medications, ex.appointments, ex.warnings, ex.instructions, ex.unclear_items]
    for group in groups:
        for item in group:
            ok, score = quote_in_document(item.source_quote, doc_norm)
            problems = [] if ok else [f"quoted text was not found in the document (match {score:.0%})"]
            if ok and isinstance(item, Medication):
                problems += _med_problems(item, normalize(item.source_quote))
                problems += _field_problems("purpose", item.purpose, doc_norm)
                for extra in item.special_instructions:
                    problems += _field_problems("instruction", extra, doc_norm)
            if getattr(item, "phone", None) and normalize(item.phone) not in doc_norm:
                problems.append(f"phone number {item.phone} is not in the document")
            if isinstance(item, Appointment):
                q = normalize(item.source_quote)
                if item.date and not any(v in q for v in _date_variants(item.date)):
                    problems.append(f"date {item.date} is not in the quoted text")
                if item.time and _to12(item.time) not in q and item.time not in q:
                    problems.append(f"time {item.time} is not in the quoted text")
                for label in ("time_window", "location", "purpose", "what_to_bring"):
                    problems += _field_problems(label, getattr(item, label), doc_norm)
            if isinstance(item, WarningGroup):
                for sign in item.signs:
                    problems += _field_problems("warning sign", sign, doc_norm)
            if isinstance(item, Instruction):
                problems += _field_problems("instruction", item.text, doc_norm)
            results[item.id] = {"verified": not problems, "score": score, "problems": problems}
    return results


# ------------------------------------------------------------------ Explainer checks

def check_explanation_numbers(text: str, allowed_source: str) -> set[str]:
    """Numbers in `text` that do not appear anywhere in `allowed_source`."""
    allowed = numbers(allowed_source)
    # Allow fraction parts and harmless digits that come from dates/times written differently.
    for frac in re.findall(r"(\d+)/(\d+)", to_ascii_digits(allowed_source)):
        allowed.update(frac)
    return {n for n in numbers(text) if n not in allowed}


# ------------------------------------------------------------------ readability

def _syllables(word: str) -> int:
    word = word.lower()
    word = re.sub(r"[^a-z]", "", word)
    if not word:
        return 0
    if len(word) <= 3:
        return 1
    word = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", word)
    word = re.sub(r"^y", "", word)
    return max(1, len(re.findall(r"[aeiouy]{1,2}", word)))


def reading_grade(text: str) -> float | None:
    """Flesch-Kincaid grade level (English only)."""
    text = re.sub(r"\s*\n\s*", " ", text or "")          # PDF line breaks are not sentence ends
    text = re.sub(r"(?<=\d)\.(?=\d)", "", text)           # 12.5 is not a sentence end
    sentences = [s for s in re.split(r"[.!?]+(?:\s|$)", text) if re.search(r"[A-Za-z]", s)]
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", text or "")
    if len(words) < 30 or not sentences:
        return None
    syl = sum(_syllables(w) for w in words)
    grade = 0.39 * (len(words) / len(sentences)) + 11.8 * (syl / len(words)) - 15.59
    return round(grade, 1)
