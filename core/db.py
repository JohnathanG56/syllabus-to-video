"""SQLite persistence (stdlib sqlite3, WAL mode).

One short-lived connection per operation keeps this safe to call from the API,
worker threads and tests alike. The jobs table doubles as the work queue
(no Redis): workers claim rows with a guarded UPDATE so two workers can never
run the same job.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS syllabi (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT,
    filename    TEXT,
    raw_text    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',   -- queued | extracting | ready | failed
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topics (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    syllabus_id         INTEGER NOT NULL REFERENCES syllabi(id) ON DELETE CASCADE,
    position            INTEGER NOT NULL,
    title               TEXT NOT NULL,
    slug                TEXT NOT NULL,
    objectives_json     TEXT NOT NULL,
    prerequisites_json  TEXT NOT NULL,
    -- queued -> scripted -> narrated -> coded -> rendering -> (repairing) -> assembled -> done | failed
    status              TEXT NOT NULL DEFAULT 'queued',
    script_json         TEXT,
    durations_json      TEXT,
    code                TEXT,
    video_path          TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    error               TEXT,
    warning             TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (syllabus_id, position)
);

CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,              -- extract_topics | process_topic
    ref_id        INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | failed
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 2,
    worker_id     TEXT,
    locked_at     REAL,
    error         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
-- at most one live job per (kind, ref): makes enqueue idempotent
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_live
    ON jobs(kind, ref_id) WHERE status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS render_attempts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id          INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    attempt_no        INTEGER NOT NULL,
    phase             TEXT NOT NULL DEFAULT 'validate',  -- validate | final
    error_class       TEXT,          -- NULL on success
    exit_reason       TEXT,
    render_seconds    REAL NOT NULL DEFAULT 0,
    success           INTEGER NOT NULL,
    error_message     TEXT,
    video_duration_s  REAL,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    syllabus_id    INTEGER,
    topic_id       INTEGER,
    task           TEXT NOT NULL,     -- topics | script | codegen | repair
    model          TEXT,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    latency_s      REAL NOT NULL DEFAULT 0,
    ok             INTEGER NOT NULL,
    created_at     TEXT NOT NULL
);
"""

TOPIC_STATES = ("queued", "scripted", "narrated", "coded", "rendering", "repairing",
                "assembled", "done", "failed")
TOPIC_FIELDS = {"status", "script_json", "durations_json", "code", "video_path", "attempts",
                "error", "warning"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    # ------------------------------------------------------------ plumbing
    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            yield conn
        finally:
            conn.close()

    def init(self) -> "Database":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.execute("PRAGMA journal_mode = WAL")
            c.executescript(SCHEMA)
        return self

    # ------------------------------------------------------------ syllabi
    def create_syllabus(self, raw_text: str, filename: str | None = None, title: str | None = None) -> int:
        ts = now_iso()
        with self.connect() as c:
            cur = c.execute(
                "INSERT INTO syllabi (title, filename, raw_text, status, created_at, updated_at)"
                " VALUES (?, ?, ?, 'queued', ?, ?)", (title, filename, raw_text, ts, ts))
            return int(cur.lastrowid)

    def get_syllabus(self, syllabus_id: int) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM syllabi WHERE id = ?", (syllabus_id,)).fetchone()
        return dict(row) if row else None

    def list_syllabi(self, limit: int = 20) -> list[dict]:
        with self.connect() as c:
            rows = c.execute("SELECT id, title, filename, status, error, created_at FROM syllabi"
                             " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def update_syllabus(self, syllabus_id: int, **fields: Any) -> None:
        allowed = {"title", "status", "error"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown syllabus fields {bad}")
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.connect() as c:
            c.execute(f"UPDATE syllabi SET {cols} WHERE id = ?", (*fields.values(), syllabus_id))

    # ------------------------------------------------------------ topics
    def insert_topics(self, syllabus_id: int, topics: list[dict]) -> list[int]:
        """Insert topics in order. Idempotent: existing positions are left alone."""
        ts = now_iso()
        ids = []
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                for pos, t in enumerate(topics, start=1):
                    c.execute(
                        "INSERT OR IGNORE INTO topics (syllabus_id, position, title, slug, objectives_json,"
                        " prerequisites_json, status, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                        (syllabus_id, pos, t["title"], t["slug"], json.dumps(t["objectives"]),
                         json.dumps(t.get("prerequisites", [])), ts, ts))
                    row = c.execute("SELECT id FROM topics WHERE syllabus_id = ? AND position = ?",
                                    (syllabus_id, pos)).fetchone()
                    ids.append(int(row["id"]))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        return ids

    def get_topic(self, topic_id: int) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
        return _topic_row(row) if row else None

    def list_topics(self, syllabus_id: int) -> list[dict]:
        with self.connect() as c:
            rows = c.execute("SELECT * FROM topics WHERE syllabus_id = ? ORDER BY position",
                             (syllabus_id,)).fetchall()
        return [_topic_row(r) for r in rows]

    def update_topic(self, topic_id: int, **fields: Any) -> None:
        bad = set(fields) - TOPIC_FIELDS
        if bad:
            raise ValueError(f"unknown topic fields {bad}")
        if "status" in fields and fields["status"] not in TOPIC_STATES:
            raise ValueError(f"invalid topic status {fields['status']!r}")
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.connect() as c:
            c.execute(f"UPDATE topics SET {cols} WHERE id = ?", (*fields.values(), topic_id))

    # ------------------------------------------------------------ job queue
    def enqueue_job(self, kind: str, ref_id: int, max_attempts: int = 2) -> int | None:
        """Returns the job id, or None if an identical job is already queued/running."""
        ts = now_iso()
        with self.connect() as c:
            try:
                cur = c.execute(
                    "INSERT INTO jobs (kind, ref_id, status, max_attempts, created_at, updated_at)"
                    " VALUES (?, ?, 'queued', ?, ?, ?)", (kind, ref_id, max_attempts, ts, ts))
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                return None

    def claim_job(self, worker_id: str, kinds: tuple[str, ...] | None = None) -> dict | None:
        """Atomically move the oldest queued job to 'running' and return it."""
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                q = "SELECT * FROM jobs WHERE status = 'queued'"
                args: tuple = ()
                if kinds:
                    q += f" AND kind IN ({', '.join('?' * len(kinds))})"
                    args = tuple(kinds)
                row = c.execute(q + " ORDER BY id LIMIT 1", args).fetchone()
                if row is None:
                    c.execute("COMMIT")
                    return None
                c.execute("UPDATE jobs SET status = 'running', worker_id = ?, locked_at = ?,"
                          " attempts = attempts + 1, updated_at = ? WHERE id = ? AND status = 'queued'",
                          (worker_id, time.time(), now_iso(), row["id"]))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        job = dict(row)
        job["status"] = "running"
        job["attempts"] += 1
        return job

    def finish_job(self, job_id: int) -> None:
        with self.connect() as c:
            c.execute("UPDATE jobs SET status = 'done', error = NULL, updated_at = ? WHERE id = ?",
                      (now_iso(), job_id))

    def fail_job(self, job_id: int, error: str, retry: bool = True) -> str:
        """Requeue if attempts remain (and retry=True), else mark failed. Returns new status."""
        with self.connect() as c:
            row = c.execute("SELECT attempts, max_attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
            status = "queued" if retry and row and row["attempts"] < row["max_attempts"] else "failed"
            c.execute("UPDATE jobs SET status = ?, error = ?, worker_id = NULL, locked_at = NULL,"
                      " updated_at = ? WHERE id = ?", (status, error[:4000], now_iso(), job_id))
        return status

    def requeue_stale(self, older_than_s: float) -> int:
        """Crash recovery: jobs stuck in 'running' (worker died) go back to the queue.
        Safe because every pipeline stage is idempotent."""
        cutoff = time.time() - older_than_s
        with self.connect() as c:
            cur = c.execute("UPDATE jobs SET status = 'queued', worker_id = NULL, locked_at = NULL,"
                            " updated_at = ? WHERE status = 'running' AND locked_at < ?",
                            (now_iso(), cutoff))
            return cur.rowcount

    def heartbeat(self, job_id: int) -> None:
        with self.connect() as c:
            c.execute("UPDATE jobs SET locked_at = ? WHERE id = ?", (time.time(), job_id))

    def list_jobs(self, status: str | None = None) -> list[dict]:
        with self.connect() as c:
            if status:
                rows = c.execute("SELECT * FROM jobs WHERE status = ? ORDER BY id", (status,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ telemetry
    def log_render_attempt(self, topic_id: int, attempt_no: int, success: bool, render_seconds: float,
                           error_class: str | None = None, exit_reason: str | None = None,
                           error_message: str | None = None, phase: str = "validate",
                           video_duration_s: float | None = None) -> None:
        with self.connect() as c:
            c.execute(
                "INSERT INTO render_attempts (topic_id, attempt_no, phase, error_class, exit_reason,"
                " render_seconds, success, error_message, video_duration_s, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (topic_id, attempt_no, phase, error_class, exit_reason, render_seconds, int(success),
                 (error_message or "")[:2000] or None, video_duration_s, now_iso()))

    def list_render_attempts(self, topic_id: int) -> list[dict]:
        with self.connect() as c:
            rows = c.execute("SELECT * FROM render_attempts WHERE topic_id = ? ORDER BY id",
                             (topic_id,)).fetchall()
        return [dict(r) for r in rows]

    def log_llm_call(self, task: str, model: str | None, input_tokens: int, output_tokens: int,
                     latency_s: float, ok: bool, syllabus_id: int | None = None,
                     topic_id: int | None = None) -> None:
        with self.connect() as c:
            c.execute(
                "INSERT INTO llm_calls (syllabus_id, topic_id, task, model, input_tokens, output_tokens,"
                " latency_s, ok, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (syllabus_id, topic_id, task, model, input_tokens, output_tokens, latency_s, int(ok),
                 now_iso()))

    def llm_usage(self, topic_id: int | None = None, syllabus_id: int | None = None) -> dict:
        q = ("SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens), 0) AS input_tokens,"
             " COALESCE(SUM(output_tokens), 0) AS output_tokens FROM llm_calls WHERE 1=1")
        args: list = []
        if topic_id is not None:
            q += " AND topic_id = ?"
            args.append(topic_id)
        if syllabus_id is not None:
            q += " AND syllabus_id = ?"
            args.append(syllabus_id)
        with self.connect() as c:
            return dict(c.execute(q, args).fetchone())


def _topic_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["objectives"] = json.loads(d.pop("objectives_json"))
    d["prerequisites"] = json.loads(d.pop("prerequisites_json"))
    d["script"] = json.loads(d["script_json"]) if d.get("script_json") else None
    d["durations"] = json.loads(d["durations_json"]) if d.get("durations_json") else None
    return d
