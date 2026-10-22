"""Background worker: polls the SQLite jobs table (no Redis) and runs jobs on a
thread pool. Renders are additionally capped by RENDER_CONCURRENCY (default 2)
via LimitedRunner, so LLM/TTS work for one topic can overlap another's render.

Run: python -m worker            (forever)
     python -m worker --drain    (exit when the queue is empty)
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

from core.config import Settings, get_settings
from core.db import Database
from core.llm import LLM, LLMOutputError
from pipeline.narration import TTS
from pipeline.orchestrator import LimitedRunner, Pipeline
from pipeline.topics import process_syllabus
from sandbox.run import Runner

log = logging.getLogger("worker")

HEARTBEAT_S = 20.0
STALE_AFTER_S = 120.0  # a running job with no heartbeat for this long is assumed dead


class Worker:
    def __init__(self, settings: Settings, llm: LLM, runner: Runner, tts: TTS, db: Database | None = None,
                 worker_id: str | None = None):
        self.settings = settings
        self.db = db or Database(settings.db_path).init()
        self.runner = runner if isinstance(runner, LimitedRunner) else LimitedRunner(
            runner, settings.render_concurrency)
        self.pipeline = Pipeline(db=self.db, llm=llm, runner=self.runner, tts=tts, settings=settings)
        self.llm = llm
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self._running: dict[int, Future] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ jobs
    def handle(self, job: dict) -> None:
        kind, ref = job["kind"], job["ref_id"]
        if kind == "extract_topics":
            try:
                topic_ids = process_syllabus(self.db, self.llm, ref, max_topics=self.settings.max_topics)
            except LLMOutputError as exc:
                self.db.update_syllabus(ref, status="failed", error=f"topic extraction failed: {exc}"[:2000])
                return
            for tid in topic_ids:
                t = self.db.get_topic(tid)
                if t and t["status"] not in ("done", "failed"):
                    self.db.enqueue_job("process_topic", tid)
        elif kind == "process_topic":
            self.pipeline.process_topic(ref)
        else:
            raise ValueError(f"unknown job kind {kind!r}")

    def execute(self, job: dict) -> None:
        log.info("job %s %s(%s) attempt %s", job["id"], job["kind"], job["ref_id"], job["attempts"])
        try:
            self.handle(job)
        except Exception as exc:  # noqa: BLE001 - infra errors (API outage, disk) are retried
            log.exception("job %s crashed", job["id"])
            status = self.db.fail_job(job["id"], f"{type(exc).__name__}: {exc}")
            if status == "failed":
                self._mark_failed(job, f"{type(exc).__name__}: {exc}")
            return
        self.db.finish_job(job["id"])

    def _mark_failed(self, job: dict, message: str) -> None:
        if job["kind"] == "extract_topics":
            self.db.update_syllabus(job["ref_id"], status="failed", error=message[:2000])
        elif job["kind"] == "process_topic":
            topic = self.db.get_topic(job["ref_id"])
            if topic and topic["status"] == "done":
                return  # another attempt already finished this topic; keep its video and clean state
            self.db.update_topic(job["ref_id"], status="failed", error=message[:2000])

    def run_once(self) -> bool:
        """Claim and run one job synchronously. Returns False if the queue was empty."""
        job = self.db.claim_job(self.worker_id)
        if job is None:
            return False
        self.execute(job)
        return True

    def drain(self) -> int:
        n = 0
        while self.run_once():
            n += 1
        return n

    # ------------------------------------------------------------------ loop
    def _heartbeat_loop(self, stop: threading.Event) -> None:
        while not stop.wait(HEARTBEAT_S):
            with self._lock:
                ids = list(self._running)
            for jid in ids:
                self.db.heartbeat(jid)

    def run_forever(self, stop: threading.Event | None = None, exit_when_idle: bool = False) -> None:
        stop = stop or threading.Event()
        recovered = self.db.requeue_stale(STALE_AFTER_S)
        if recovered:
            log.warning("requeued %d stale job(s) from a previous run", recovered)
        hb = threading.Thread(target=self._heartbeat_loop, args=(stop,), daemon=True)
        hb.start()
        threads = max(1, self.settings.worker_threads)
        log.info("worker %s: %d threads, %d concurrent renders, backend=%s", self.worker_id, threads,
                 self.runner.max_concurrent, self.runner.inner.name)
        with ThreadPoolExecutor(max_workers=threads, thread_name_prefix="job") as pool:
            while not stop.is_set():
                with self._lock:
                    for jid in [j for j, f in self._running.items() if f.done()]:
                        del self._running[jid]
                    busy = len(self._running)
                job = self.db.claim_job(self.worker_id) if busy < threads else None
                if job is not None:
                    fut = pool.submit(self.execute, job)
                    with self._lock:
                        self._running[job["id"]] = fut
                    continue
                if exit_when_idle and busy == 0:
                    break
                stop.wait(self.settings.worker_poll_s)
                self.db.requeue_stale(STALE_AFTER_S)
        stop.set()


def build_worker(settings: Settings | None = None) -> Worker:
    from core.llm import get_llm
    from pipeline.narration import get_tts
    from sandbox.run import docker_available, get_runner, image_available

    settings = settings or get_settings()
    if settings.render_backend == "docker":
        if not docker_available():
            raise SystemExit("RENDER_BACKEND=docker but the Docker daemon is not reachable. Start Docker "
                             "Desktop, or set RENDER_BACKEND=local (UNSAFE, dev only).")
        if not image_available(settings.sandbox_image):
            raise SystemExit(f"Sandbox image {settings.sandbox_image} not found. Build it with:\n"
                             f"  docker build -t {settings.sandbox_image} sandbox/")
    else:
        log.warning("RENDER_BACKEND=local: LLM-written code runs on this machine WITHOUT a sandbox.")
    return Worker(settings, llm=get_llm(settings), runner=get_runner(settings), tts=get_tts(settings))
