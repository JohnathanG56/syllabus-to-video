"""HTTP API + static web UI.

Run: uvicorn api.main:create_app --factory --port 8000
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from core.config import Settings, get_settings
from core.db import Database
from pipeline.ingest import IngestError, extract_text

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
TERMINAL = {"done", "failed"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    db = Database(settings.db_path).init()
    out_root = settings.out_dir.resolve()
    app = FastAPI(title="Syllabus-to-Video", version="0.1.0")
    app.state.db = db
    app.state.settings = settings

    @app.get("/health")
    def health():
        return {"ok": True, "render_backend": settings.render_backend}

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

    @app.get("/syllabi")
    def list_syllabi(limit: int = 20):
        return db.list_syllabi(limit=min(max(limit, 1), 100))

    @app.get("/syllabi/{syllabus_id}")
    def get_syllabus(syllabus_id: int):
        syl = db.get_syllabus(syllabus_id)
        if syl is None:
            raise HTTPException(404, "syllabus not found")
        topics = [_topic_view(t) for t in db.list_topics(syllabus_id)]
        done = sum(t["status"] == "done" for t in topics)
        failed = sum(t["status"] == "failed" for t in topics)
        finished = syl["status"] == "failed" or (syl["status"] == "ready" and done + failed == len(topics))
        return {
            "id": syl["id"],
            "title": syl["title"],
            "filename": syl["filename"],
            "status": syl["status"],
            "error": syl["error"],
            "created_at": syl["created_at"],
            "progress": {"total": len(topics), "done": done, "failed": failed, "finished": finished},
            "topics": topics,
        }

    @app.get("/topics/{topic_id}")
    def get_topic(topic_id: int):
        t = db.get_topic(topic_id)
        if t is None:
            raise HTTPException(404, "topic not found")
        view = _topic_view(t)
        view["script"] = t["script"]
        view["durations"] = t["durations"]
        view["code"] = t["code"]
        view["render_attempts"] = db.list_render_attempts(topic_id)
        view["llm_usage"] = db.llm_usage(topic_id=topic_id)
        return view

    @app.get("/videos/{path:path}")
    def get_video(path: str):
        target = (out_root / path).resolve()
        # path traversal guard: only files under OUT_DIR, only our output types
        if not target.is_relative_to(out_root) or target.suffix not in {".mp4", ".srt", ".vtt"}:
            raise HTTPException(404, "not found")
        if target.suffix == ".vtt":  # browsers' <track> wants WebVTT; derive it from the .srt
            srt = target.with_suffix(".srt")
            if not srt.is_file():
                raise HTTPException(404, "not found")
            vtt = "WEBVTT\n\n" + re.sub(r"(\d\d:\d\d:\d\d),(\d\d\d)", r"\1.\2", srt.read_text())
            return PlainTextResponse(vtt, media_type="text/vtt")
        if not target.is_file():
            raise HTTPException(404, "not found")
        media = "video/mp4" if target.suffix == ".mp4" else "application/x-subrip"
        return FileResponse(target, media_type=media)

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(WEB_DIR / "index.html")

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
        "video_url": f"/videos/{t['video_path']}" if t.get("video_path") and t["status"] == "done" else None,
        "captions_url": (f"/videos/{t['video_path'][:-4]}.vtt"
                         if t.get("video_path") and t["status"] == "done" else None),
    }
