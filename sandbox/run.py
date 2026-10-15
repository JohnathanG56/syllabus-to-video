"""Host-side render runners.

LLM-written Manim code is untrusted. ``DockerRunner`` is the real security
boundary: no network, read-only root FS, memory/CPU/PID caps, no capabilities,
no-new-privileges, and a host-side timeout that kills the container.

``LocalRunner`` exists for development on machines without Docker. It applies
best-effort rlimits and a timeout, strips secrets from the environment, but
it is **NOT A SANDBOX**: the code runs as your user, with your filesystem and
network. Never point it at untrusted model output you have not read.

Both return the same structured ``RenderResult``; select with
``RENDER_BACKEND=docker|local``.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path

ENTRY_SCRIPT = Path(__file__).resolve().parent / "render_entry.py"
CONTAINER_ENTRY = "/opt/stv/render_entry.py"
STDERR_TAIL_LINES = 60

EXIT_OK = "ok"
EXIT_ERROR = "error"
EXIT_TIMEOUT = "timeout"
EXIT_OOM = "oom"


@dataclass
class RenderResult:
    success: bool
    video_path: Path | None
    stderr_tail: str
    duration_s: float  # wall-clock render time
    exit_reason: str  # ok | error | timeout | oom
    error_type: str | None = None  # exception class raised inside the sandbox
    error_message: str | None = None
    returncode: int | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["video_path"] = str(self.video_path) if self.video_path else None
        return d


def tail_lines(text: str, n: int = STDERR_TAIL_LINES) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def classify_exit(returncode: int | None, timed_out: bool, error_type: str | None, ok: bool) -> str:
    """Map process outcome to an exit_reason. 137 = SIGKILL, which inside a
    memory-capped container (and not caused by our timeout) means the OOM killer."""
    if timed_out:
        return EXIT_TIMEOUT
    if ok and returncode == 0:
        return EXIT_OK
    if error_type == "MemoryError":
        return EXIT_OOM
    if returncode in (137, -9, -int(signal.SIGKILL)):
        return EXIT_OOM
    return EXIT_ERROR


def _read_result_json(out_dir: Path) -> dict:
    try:
        return json.loads((out_dir / "result.json").read_text())
    except (OSError, ValueError):
        return {}


def _find_video(out_dir: Path) -> Path | None:
    vids = [p for p in out_dir.rglob("*.mp4") if "partial_movie_files" not in p.parts]
    return max(vids, key=lambda p: p.stat().st_mtime) if vids else None


class Runner(ABC):
    name = "abstract"

    @abstractmethod
    def render(self, scene_file: Path, out_dir: Path, quality: str = "l",
               timeout_s: float = 180, scene_name: str = "LessonScene") -> RenderResult:
        ...

    def _finish(self, out_dir: Path, output: str, returncode: int | None, timed_out: bool,
                started: float, path_map=None) -> RenderResult:
        info = _read_result_json(out_dir) if not timed_out else {}
        ok = bool(info.get("ok")) and returncode == 0
        video = None
        if ok:
            raw = info.get("video")
            if raw and path_map:
                raw = path_map(raw)
            video = Path(raw) if raw and Path(raw).exists() else _find_video(out_dir)
            ok = video is not None
        reason = classify_exit(returncode, timed_out, info.get("error_type"), ok)
        return RenderResult(
            success=reason == EXIT_OK,
            video_path=video if reason == EXIT_OK else None,
            stderr_tail=tail_lines(output),
            duration_s=round(time.monotonic() - started, 3),
            exit_reason=reason,
            error_type=info.get("error_type") or ("Timeout" if timed_out else None),
            error_message=info.get("error_message"),
            returncode=returncode,
        )


class DockerRunner(Runner):
    """Render inside a locked-down, throwaway container."""

    name = "docker"

    def __init__(self, image: str, memory: str = "2g", cpus: str = "2", pids_limit: int = 256,
                 docker_bin: str = "docker", tmpfs_size: str = "512m"):
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.docker_bin = docker_bin
        self.tmpfs_size = tmpfs_size

    def build_command(self, scene_file: Path, out_dir: Path, quality: str, container_name: str,
                      scene_name: str = "LessonScene") -> list[str]:
        return [
            self.docker_bin, "run", "--rm",
            "--name", container_name,
            "--network", "none",
            "--memory", self.memory, "--memory-swap", self.memory,
            "--cpus", str(self.cpus),
            "--pids-limit", str(self.pids_limit),
            "--read-only",
            "--tmpfs", f"/tmp:rw,size={self.tmpfs_size}",
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            "-e", "HOME=/tmp",
            "-v", f"{Path(scene_file).resolve()}:/work/scene.py:ro",
            "-v", f"{ENTRY_SCRIPT}:{CONTAINER_ENTRY}:ro",
            "-v", f"{Path(out_dir).resolve()}:/out",
            self.image,
            "python", CONTAINER_ENTRY, "/work/scene.py", "/out", quality, scene_name,
        ]

    def render(self, scene_file: Path, out_dir: Path, quality: str = "l",
               timeout_s: float = 180, scene_name: str = "LessonScene") -> RenderResult:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        # the container runs as an unprivileged uid that differs from ours
        os.chmod(out_dir, 0o777)
        name = f"stv-render-{uuid.uuid4().hex[:12]}"
        cmd = self.build_command(Path(scene_file), out_dir, quality, name, scene_name)
        started = time.monotonic()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                errors="replace")
        timed_out = False
        try:
            output, _ = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Killing the docker CLI does not stop the container: kill it by name.
            subprocess.run([self.docker_bin, "kill", name], capture_output=True, timeout=30)
            subprocess.run([self.docker_bin, "rm", "-f", name], capture_output=True, timeout=30)
            try:
                output, _ = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                output, _ = proc.communicate()
        out_resolved = str(out_dir.resolve())

        def to_host(p: str) -> str:
            return out_resolved + p[len("/out"):] if p.startswith("/out") else p

        return self._finish(out_dir, output or "", proc.returncode, timed_out, started, to_host)


class LocalRunner(Runner):
    """UNSAFE, dev-only: runs the scene in a plain subprocess on this machine.

    Mitigations (NOT a security boundary): wall-clock timeout that kills the
    whole process group, RLIMIT_AS/CPU/FSIZE where the OS supports them, and an
    environment scrubbed of secrets (no ANTHROPIC_API_KEY etc.).
    """

    name = "local"
    SAFE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR", "PYTHONPATH",
                     "FONTCONFIG_PATH", "FONTCONFIG_FILE", "LD_LIBRARY_PATH")

    def __init__(self, python: str | None = None, memory_bytes: int = 4 * 1024**3,
                 max_file_bytes: int = 2 * 1024**3):
        self.python = python or sys.executable
        self.memory_bytes = memory_bytes
        self.max_file_bytes = max_file_bytes

    def _limits(self, timeout_s: float):
        mem, fsize = self.memory_bytes, self.max_file_bytes
        cpu = int(timeout_s * 2) + 5  # CPU seconds (multi-threaded code burns faster than wall time)

        def apply():  # runs in the child between fork and exec
            try:
                import resource
                for res, val in ((resource.RLIMIT_AS, mem), (resource.RLIMIT_CPU, cpu),
                                 (resource.RLIMIT_FSIZE, fsize)):
                    try:
                        resource.setrlimit(res, (val, val))
                    except (ValueError, OSError):
                        pass  # e.g. RLIMIT_AS is not enforceable on macOS
            except ImportError:  # pragma: no cover - non-POSIX
                pass

        return apply

    def render(self, scene_file: Path, out_dir: Path, quality: str = "l",
               timeout_s: float = 180, scene_name: str = "LessonScene") -> RenderResult:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k in self.SAFE_ENV_KEYS}
        env["HOME"] = str(out_dir)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        cmd = [self.python, str(ENTRY_SCRIPT), str(Path(scene_file).resolve()), str(out_dir.resolve()),
               quality, scene_name]
        started = time.monotonic()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                errors="replace", cwd=out_dir, env=env, start_new_session=True,
                                preexec_fn=self._limits(timeout_s))
        timed_out = False
        try:
            output, _ = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            output, _ = proc.communicate()
        rc = proc.returncode
        if rc is not None and rc < 0 and rc == -signal.SIGXCPU:
            timed_out = True  # CPU rlimit hit: treat as timeout
        return self._finish(out_dir, output or "", rc, timed_out, started)


def docker_available(docker_bin: str = "docker") -> bool:
    if shutil.which(docker_bin) is None:
        return False
    try:
        r = subprocess.run([docker_bin, "info", "--format", "{{.ServerVersion}}"],
                           capture_output=True, timeout=15)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def image_available(image: str, docker_bin: str = "docker") -> bool:
    try:
        r = subprocess.run([docker_bin, "image", "inspect", image], capture_output=True, timeout=15)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def get_runner(settings=None) -> Runner:
    if settings is None:
        from core.config import get_settings
        settings = get_settings()
    backend = settings.render_backend
    if backend == "docker":
        return DockerRunner(settings.sandbox_image)
    if backend == "local":
        return LocalRunner()
    raise ValueError(f"RENDER_BACKEND must be 'docker' or 'local', got {backend!r}")


if __name__ == "__main__":  # manual smoke test: python -m sandbox.run scene.py outdir
    import argparse

    ap = argparse.ArgumentParser(description="Render one scene file through the configured runner")
    ap.add_argument("scene")
    ap.add_argument("out")
    ap.add_argument("--quality", default="l")
    ap.add_argument("--timeout", type=float, default=180)
    ap.add_argument("--backend", choices=["docker", "local"])
    a = ap.parse_args()
    if a.backend:
        os.environ["RENDER_BACKEND"] = a.backend
    res = get_runner().render(Path(a.scene), Path(a.out), a.quality, a.timeout)
    print(json.dumps(res.to_dict(), indent=2))
