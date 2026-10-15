from pathlib import Path

import pytest

from core.db import Database
from core.llm import ScriptedLLM
from core.text import slugify
from pipeline.ingest import IngestError, clean_text, extract_text
from pipeline.topics import extract_topics, process_syllabus
from tests.fakes import TOPICS, dumps

FIX = Path(__file__).parent / "fixtures"


# ------------------------------------------------------------- ingest
def test_extract_text_from_plain_text():
    text = extract_text("s.txt", (FIX / "sample_syllabus.txt").read_bytes())
    assert "Eigenvalues and eigenvectors" in text


def test_extract_text_from_pdf():
    text = extract_text("syllabus.pdf", (FIX / "sample_syllabus.pdf").read_bytes())
    assert "Linear Algebra" in text
    assert "determinant" in text


def test_pdf_detected_by_magic_bytes_not_extension():
    text = extract_text("upload.bin", (FIX / "sample_syllabus.pdf").read_bytes())
    assert "Eigenvalues" in text


def test_corrupt_pdf_raises_ingest_error():
    with pytest.raises(IngestError):
        extract_text("x.pdf", b"%PDF-1.4 garbage garbage")


def test_empty_text_raises():
    with pytest.raises(IngestError):
        extract_text("x.txt", b"   \n\n  ")


def test_clean_text_rejoins_hyphenation_and_drops_page_numbers():
    raw = "Linear trans-\nformations\n\n\n\nPage 3 of 9\n12\nEigen   values\x07"
    out = clean_text(raw)
    assert "transformations" in out
    assert "Page 3" not in out and "\n12\n" not in out
    assert "Eigen values" in out
    assert "\x07" not in out


def test_clean_text_truncates_long_input():
    out = clean_text("line\n" * 20000, max_chars=1000)
    assert len(out) < 1100 and out.endswith("[... truncated ...]")


def test_slugify():
    assert slugify("Eigenvectors & Eigenvalues!") == "eigenvectors-eigenvalues"
    assert slugify("Café: Gödel's theorem") == "cafe-godel-s-theorem"
    assert slugify("!!!") == "topic"
    assert len(slugify("a" * 200)) <= 60


# ------------------------------------------------------------- topics
def test_extract_topics_puts_syllabus_in_prompt_and_caps_count():
    llm = ScriptedLLM(responses=[dumps(TOPICS)])
    out = extract_topics(llm, "WEEK 1: vectors", max_topics=2)
    assert [t.title for t in out.topics] == ["Vectors as arrows", "Linear transformations"]
    assert "WEEK 1: vectors" in llm.calls[0]["messages"][0]["content"]
    assert "at most 2 topics" in llm.calls[0]["system"]


def test_extract_topics_dedupes_titles():
    dup = dict(TOPICS, topics=TOPICS["topics"] + [dict(TOPICS["topics"][0], title="VECTORS AS ARROWS")])
    out = extract_topics(ScriptedLLM(responses=[dumps(dup)]), "x" * 100, max_topics=10)
    assert len(out.topics) == 3


def test_process_syllabus_stores_topics_and_is_idempotent(tmp_path):
    db = Database(tmp_path / "t.sqlite").init()
    sid = db.create_syllabus("syllabus text " * 10, filename="s.txt")
    llm = ScriptedLLM(responses=[dumps(TOPICS)])
    ids = process_syllabus(db, llm, sid, max_topics=8)
    assert len(ids) == 3
    syl = db.get_syllabus(sid)
    assert syl["status"] == "ready" and syl["title"] == "MATH 33A: Linear Algebra"
    topics = db.list_topics(sid)
    assert [t["position"] for t in topics] == [1, 2, 3]
    assert topics[0]["slug"] == "vectors-as-arrows"
    assert topics[1]["prerequisites"] == ["Vectors as arrows"]
    assert all(t["status"] == "queued" for t in topics)
    # second run: no LLM call, same ids
    assert process_syllabus(db, ScriptedLLM(responses=[]), sid) == ids
    usage = db.llm_usage(syllabus_id=sid)
    assert usage["calls"] == 1 and usage["input_tokens"] > 0


def test_process_syllabus_unique_slugs(tmp_path):
    db = Database(tmp_path / "t.sqlite").init()
    sid = db.create_syllabus("text " * 20)
    payload = dict(TOPICS, topics=[dict(TOPICS["topics"][0], title="Vectors: arrows"),
                                   dict(TOPICS["topics"][0], title="Vectors arrows")])
    process_syllabus(db, ScriptedLLM(responses=[dumps(payload)]), sid)
    slugs = [t["slug"] for t in db.list_topics(sid)]
    assert slugs == ["vectors-arrows", "vectors-arrows-2"]
