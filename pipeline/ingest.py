"""Stage 0: turn an uploaded syllabus (PDF or text) into clean plain text."""
from __future__ import annotations

import io
import re
import unicodedata

MAX_CHARS = 40_000  # bounds LLM cost; syllabi are rarely longer


class IngestError(ValueError):
    pass


def extract_text(filename: str | None, data: bytes) -> str:
    is_pdf = data[:5] == b"%PDF-" or (filename or "").lower().endswith(".pdf")
    if is_pdf:
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            pages = [(p.extract_text() or "") for p in reader.pages]
        except Exception as exc:  # pypdf raises many types on corrupt files
            raise IngestError(f"could not read PDF: {exc}") from exc
        text = "\n\n".join(pages)
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
    text = clean_text(text)
    if len(text) < 40:
        raise IngestError("no extractable text found (scanned PDF? OCR is not supported)")
    return text


_PAGE_NO = re.compile(r"^\s*(page\s*)?\d+(\s*(of|/)\s*\d+)?\s*$", re.IGNORECASE)


def clean_text(text: str, max_chars: int = MAX_CHARS) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)  # re-join words hyphenated across lines
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t\f\v]+", " ", line).strip()
        if _PAGE_NO.match(line):
            continue  # bare page numbers / "Page 3 of 9"
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0] + "\n[... truncated ...]"
    return text
