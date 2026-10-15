"""Stage 1: syllabus text -> ordered topics (LLM, validated)."""
from __future__ import annotations

from core import prompts
from core.db import Database
from core.llm import LLM, UsageHook, complete_json
from core.schemas import TopicList
from core.text import slugify


def extract_topics(llm: LLM, syllabus_text: str, max_topics: int = 8,
                   on_usage: UsageHook | None = None) -> TopicList:
    system, user = prompts.build("topics", syllabus=syllabus_text, max_topics=max_topics)
    result = complete_json(llm, system=system, user=user, schema=TopicList, task="topics",
                           max_tokens=4000, on_usage=on_usage)
    # dedupe (case-insensitive) and cap, preserving order
    seen, kept = set(), []
    for t in result.topics:
        key = t.title.casefold()
        if key not in seen:
            seen.add(key)
            kept.append(t)
    result.topics = kept[:max_topics]
    return result


def process_syllabus(db: Database, llm: LLM, syllabus_id: int, max_topics: int = 8) -> list[int]:
    """Idempotent: if topics already exist for this syllabus, returns their ids."""
    existing = db.list_topics(syllabus_id)
    if existing:
        db.update_syllabus(syllabus_id, status="ready")
        return [t["id"] for t in existing]
    syl = db.get_syllabus(syllabus_id)
    if syl is None:
        raise LookupError(f"syllabus {syllabus_id} not found")
    db.update_syllabus(syllabus_id, status="extracting")

    def usage(task, resp, ok):
        db.log_llm_call(task, resp.model, resp.input_tokens, resp.output_tokens, resp.latency_s, ok,
                        syllabus_id=syllabus_id)

    result = extract_topics(llm, syl["raw_text"], max_topics=max_topics, on_usage=usage)
    used_slugs: set[str] = set()
    rows = []
    for t in result.topics:
        slug = slugify(t.title)
        base, n = slug, 2
        while slug in used_slugs:
            slug = f"{base}-{n}"
            n += 1
        used_slugs.add(slug)
        rows.append({"title": t.title, "slug": slug, "objectives": t.objectives,
                     "prerequisites": t.prerequisites})
    ids = db.insert_topics(syllabus_id, rows)
    db.update_syllabus(syllabus_id, status="ready", title=result.course_title)
    return ids
