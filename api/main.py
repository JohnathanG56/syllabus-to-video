"""HTTP API. Run: uvicorn api.main:create_app --factory --reload"""
from __future__ import annotations

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from core.config import Settings, get_settings
from core.db import Database
from pipeline.ingest import IngestError, extract_text

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    db = Database(settings.db_path).init()
    app = FastAPI(title="Syllabus-to-Video", version="0.1.0")
    app.state.db = db
    app.state.settings = settings

    @app.post("/syllabi", status_code=201)
    async def upload_syllabus(file: UploadFile | None = File(default=None),
                              text: str | None = Form(default=None)):
        if file is None and not text:
            raise HTTPException(400, "send a PDF/text file as 'file' or raw text as 'text'")
        if file is not None:
            data = await file.read(MAX_UPLOAD_BYTES + 1)
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "file too large (max 10 MB)")
            filename = file.filename
        else:
            data, filename = text.encode("utf-8"), None
        try:
            clean = extract_text(filename, data)
        except IngestError as exc:
            raise HTTPException(422, str(exc)) from exc
        syllabus_id = db.create_syllabus(clean, filename=filename)
        db.enqueue_job("extract_topics", syllabus_id)
        return {"id": syllabus_id, "status": "queued"}

    @app.get("/syllabi/{syllabus_id}")
    def get_syllabus(syllabus_id: int):
        syl = db.get_syllabus(syllabus_id)
        if syl is None:
            raise HTTPException(404, "syllabus not found")
        topics = db.list_topics(syllabus_id)
        return {
            "id": syl["id"],
            "title": syl["title"],
            "filename": syl["filename"],
            "status": syl["status"],
            "error": syl["error"],
            "created_at": syl["created_at"],
            "topics": [_topic_view(t) for t in topics],
        }

    return app


def _topic_view(t: dict) -> dict:
    return {
        "id": t["id"],
        "position": t["position"],
        "title": t["title"],
        "slug": t["slug"],
        "objectives": t["objectives"],
        "prerequisites": t["prerequisites"],
        "status": t["status"],
        "attempts": t["attempts"],
        "error": t["error"],
        "warning": t["warning"],
    }

