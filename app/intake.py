"""Turn an upload into clean document text, and stop attacks before any model sees them.

Defenses here (all deterministic, no model involved):
1. File limits: type, size, page count, text length.
2. Hidden text: characters that are white or tiny in a PDF are removed and reported.
3. Prompt-injection lines: lines that talk to an AI instead of to a patient are removed and reported.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import pdfplumber

from . import config
from .llm import Image


class IntakeError(ValueError):
    pass


@dataclass
class Document:
    text: str = ""                                  # cleaned text that models and checks use
    images: list[Image] = field(default_factory=list)   # photos / scanned pages still needing transcription
    pages: int = 0
    hidden_text: list[str] = field(default_factory=list)
    removed_lines: list[str] = field(default_factory=list)
    source_kind: str = "pdf"


ALLOWED = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".txt": "text/plain",
}

INJECTION_PATTERNS = [
    r"ignore (all |any )?(the )?(previous|prior|above|earlier) (instructions|rules|prompts?)",
    r"disregard (all |any |your |the )+(instructions|rules|safety|checker|guidelines)",
    r"\b(system|developer) (override|prompt|message|instructions?)\b",
    # Needs an explicit AI word: "physician assistant should call you" is normal discharge text.
    r"\b(ai|a\.i\.|language model|llm|chatbot|ai assistant)\b.{0,40}\b(reading|must|should|you are|disregard|ignore)\b",
    # Role-change phrasing only: "you are now on apixaban" is a real medicine instruction.
    r"\byou are now (a|an|in|acting|operating|playing|my|the|free|unrestricted|dan)\b",
    r"\bmaintenance mode\b",
    r"\bjailbreak\b",
    r"mark (every|all|each) (item|items|medication|medications|field)s? as (verified|correct|checked)",
    r"\b(reveal|print|show) (your|the) (system )?prompt\b",
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in INJECTION_PATTERNS), re.IGNORECASE)


def _color_vals(color) -> list[float] | None:
    if color is None:
        return None
    if isinstance(color, (int, float)):
        color = (color,)
    try:
        return [float(c) for c in color]
    except (TypeError, ValueError):
        return None


def _is_whiteish(color) -> bool:
    vals = _color_vals(color)
    if not vals:
        return False
    if len(vals) in (1, 3):           # gray or RGB: near white
        return all(v >= 0.95 for v in vals)
    if len(vals) == 4:                # CMYK: no ink
        return all(v <= 0.05 for v in vals)
    return False


def _hidden_predicate(page):
    """A char is hidden if it is tiny, or white with no dark filled box behind it."""
    dark_boxes = [
        (r["x0"], r["top"], r["x1"], r["bottom"])
        for r in page.rects
        if r.get("fill") and not _is_whiteish(r.get("non_stroking_color"))
    ]

    def on_dark_box(o: dict) -> bool:
        cx, cy = (o["x0"] + o["x1"]) / 2, (o["top"] + o["bottom"]) / 2
        return any(x0 <= cx <= x1 and t <= cy <= b for x0, t, x1, b in dark_boxes)

    def hidden(o: dict) -> bool:
        if o.get("object_type") != "char":
            return False
        if float(o.get("size", 10)) < 4:
            return True
        return _is_whiteish(o.get("non_stroking_color")) and not on_dark_box(o)

    return hidden


def scan_injection(text: str) -> tuple[str, list[str]]:
    """Remove sentences that address an AI. Returns (clean_text, removed_text).

    PDF text wraps mid-sentence, so once a line matches, the following lines are removed
    too until the sentence ends.
    """
    kept, removed = [], []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if _INJECTION_RE.search(line):
            chunk = [line.strip()]
            while not re.search(r"[.!?]\s*$", lines[i]) and i + 1 < len(lines):
                i += 1
                chunk.append(lines[i].strip())
            removed.append(" ".join(chunk))
            kept.append("[line removed by safety filter]")
        else:
            kept.append(line)
        i += 1
    return "\n".join(kept), removed


def _from_pdf(data: bytes) -> Document:
    doc = Document(source_kind="pdf")
    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001 - any parse failure is a bad upload
        raise IntakeError(f"This PDF could not be opened ({e.__class__.__name__}).")
    with pdf:
        doc.pages = len(pdf.pages)
        if doc.pages > config.MAX_PAGES:
            raise IntakeError(f"This PDF has {doc.pages} pages. The limit is {config.MAX_PAGES}.")
        texts = []
        for page in pdf.pages:
            is_hidden = _hidden_predicate(page)
            visible = page.filter(lambda o: not is_hidden(o)).extract_text() or ""
            hidden = page.filter(is_hidden).extract_text() or ""
            if hidden.strip():
                doc.hidden_text.append(" ".join(hidden.split()))
            texts.append(visible)
        doc.text = "\n".join(texts).strip()
        if len(doc.text) < 40:
            # Scanned PDF: no text layer, so send page images to the transcriber.
            doc.source_kind = "scanned_pdf"
            for page in pdf.pages:
                buf = io.BytesIO()
                page.to_image(resolution=150).original.save(buf, format="PNG")
                doc.images.append(Image(buf.getvalue(), "image/png"))
            doc.text = ""
    return doc


def load(filename: str, data: bytes) -> Document:
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED:
        raise IntakeError("Please upload a PDF, a photo (PNG or JPG), or a text file.")
    if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise IntakeError(f"The file is larger than {config.MAX_UPLOAD_MB} MB.")
    if not data:
        raise IntakeError("The file is empty.")

    if ext == ".pdf":
        if not data.startswith(b"%PDF"):
            raise IntakeError("This file is not a valid PDF.")
        doc = _from_pdf(data)
    elif ext == ".txt":
        doc = Document(text=data.decode("utf-8", errors="replace"), pages=1, source_kind="text")
    else:
        doc = Document(images=[Image(data, ALLOWED[ext])], pages=1, source_kind="photo")

    if doc.text:
        finalize_text(doc, doc.text)
    return doc


def finalize_text(doc: Document, text: str) -> None:
    """Apply the length cap and the injection scan to document text (also used after transcription)."""
    if len(text) > config.MAX_DOC_CHARS:
        raise IntakeError(f"The document is too long ({len(text):,} characters; limit {config.MAX_DOC_CHARS:,}).")
    clean, removed = scan_injection(text)
    doc.text = clean
    doc.removed_lines.extend(removed)
