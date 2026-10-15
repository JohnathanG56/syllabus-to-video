"""python -m demo  - run the whole pipeline on a 2-topic demo syllabus with a
fake LLM that returns hand-written scripts and Manim code. Everything else is
real: Piper narration, AST validation, sandboxed Manim renders (one deliberate
manimgl-style API error to exercise the self-repair loop), ffmpeg assembly.

Outputs go to out/demo/<syllabus_id>/<n>-<slug>.mp4 (DB in data/demo/).
Watch them in the UI with:
  DATA_DIR=data/demo OUT_DIR=out/demo uvicorn api.main:create_app --factory
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Offline end-to-end demo (no API key needed)")
    ap.add_argument("--fresh", action="store_true", help="delete previous demo data/output first")
    ap.add_argument("--no-repair", action="store_true", help="skip the deliberate first-try failure")
    ap.add_argument("--backend", choices=["docker", "local"], help="override RENDER_BACKEND")
    ap.add_argument("--silent", action="store_true", help="silent narration instead of Piper")
    a = ap.parse_args(argv)

    os.environ.setdefault("DATA_DIR", str(ROOT / "data" / "demo"))
    os.environ.setdefault("OUT_DIR", str(ROOT / "out" / "demo"))
    if a.backend:
        os.environ["RENDER_BACKEND"] = a.backend
    if a.silent:
        os.environ["TTS_BACKEND"] = "silent"

    from core.config import get_settings
    from core.db import Database
    from demo.content import DEMO_SYLLABUS
    from demo.llm import demo_llm
    from pipeline.ingest import extract_text
    from pipeline.media import probe_duration, probe_streams
    from pipeline.narration import get_tts
    from sandbox.run import docker_available, get_runner, image_available
    from worker.main import Worker

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if a.fresh:
        shutil.rmtree(settings.data_dir, ignore_errors=True)
        shutil.rmtree(settings.out_dir, ignore_errors=True)
    if settings.render_backend == "docker" and not (docker_available() and image_available(settings.sandbox_image)):
        print(f"Docker sandbox not ready (daemon down or image {settings.sandbox_image} missing).\n"
              f"Build it with `docker build -t {settings.sandbox_image} sandbox/` or run with --backend local "
              "(UNSAFE for untrusted code; fine for this demo's hand-written code).", file=sys.stderr)
        return 2

    db = Database(settings.db_path).init()
    sid = db.create_syllabus(extract_text("demo.txt", DEMO_SYLLABUS.encode()), filename="demo_syllabus.txt")
    db.enqueue_job("extract_topics", sid)
    worker = Worker(settings, llm=demo_llm(show_repair=not a.no_repair), runner=get_runner(settings),
                    tts=get_tts(settings), db=db, worker_id="demo")
    t0 = time.monotonic()
    stop = threading.Event()
    worker.run_forever(stop, exit_when_idle=True)
    elapsed = time.monotonic() - t0

    print(f"\n=== demo finished in {elapsed:.1f}s (backend={settings.render_backend}, "
          f"tts={type(worker.pipeline.tts).__name__}) ===")
    ok = True
    for t in db.list_topics(sid):
        attempts = db.list_render_attempts(t["id"])
        line = " -> ".join(f"#{x['attempt_no']}:{x['phase']}:{'ok' if x['success'] else x['error_class']}"
                           for x in attempts)
        print(f"[{t['status']}] {t['title']}: {line}")
        if t["status"] != "done":
            ok = False
            print(f"    error: {t['error']}")
            continue
        path = settings.out_dir / t["video_path"]
        kinds = sorted(s["codec_type"] for s in probe_streams(path))
        print(f"    {path}  {probe_duration(path):.2f}s  {path.stat().st_size / 1e6:.2f} MB  streams={kinds}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
