"""Sandbox runner tests.

Pure-logic tests always run. ``manim``-marked tests render real scenes with the
(unsafe) LocalRunner. ``docker``-marked tests exercise the real security
boundary and only run when a Docker daemon and the sandbox image exist.
"""
import os
import time
from pathlib import Path

import pytest

from sandbox.run import (
    ENTRY_SCRIPT, DockerRunner, LocalRunner, classify_exit, get_runner, tail_lines,
)

HELLO = """
from manim import *

class LessonScene(Scene):
    def construct(self):
        c = Circle(color=BLUE)
        self.play(Create(c), run_time=0.5)
        self.wait(0.5)
"""

INFINITE = """
from manim import *

class LessonScene(Scene):
    def construct(self):
        while True:
            pass
"""

NAME_ERROR = """
from manim import *

class LessonScene(Scene):
    def construct(self):
        self.play(ShowCreation(Circle()))
"""

NETWORK = """
import socket
from manim import *

class LessonScene(Scene):
    def construct(self):
        socket.create_connection(("1.1.1.1", 80), timeout=5)
"""

READS_SECRET = """
import os
from manim import *

class LessonScene(Scene):
    def construct(self):
        if "ANTHROPIC_API_KEY" in os.environ:
            raise RuntimeError("secret leaked into sandbox")
        self.wait(0.2)
"""

WRITES_ROOT = """
from manim import *

class LessonScene(Scene):
    def construct(self):
        open("/work/pwned.txt", "w").write("x")
"""

MANY_THREADS = """
import threading, time
from manim import *

class LessonScene(Scene):
    def construct(self):
        ts = []
        for _ in range(1000):
            t = threading.Thread(target=time.sleep, args=(5,))
            t.start()
            ts.append(t)
"""

MEMORY_HOG = """
from manim import *

class LessonScene(Scene):
    def construct(self):
        blocks = []
        while True:
            blocks.append(bytearray(256 * 1024 * 1024))
"""


def write(tmp_path: Path, code: str) -> Path:
    p = tmp_path / "scene.py"
    p.write_text(code)
    return p


# ---------------------------------------------------------------- pure logic
def test_tail_lines_keeps_last_n():
    text = "\n".join(str(i) for i in range(100))
    assert tail_lines(text, 3) == "97\n98\n99"
    assert tail_lines("a\nb", 60) == "a\nb"


@pytest.mark.parametrize("rc,timed_out,etype,ok,expected", [
    (0, False, None, True, "ok"),
    (1, False, "NameError", False, "error"),
    (None, True, None, False, "timeout"),
    (137, True, None, False, "timeout"),  # our own kill wins over 137
    (137, False, None, False, "oom"),
    (1, False, "MemoryError", False, "oom"),
    (0, False, None, False, "error"),  # exit 0 but no video produced
])
def test_classify_exit(rc, timed_out, etype, ok, expected):
    assert classify_exit(rc, timed_out, etype, ok) == expected


def test_docker_command_has_all_hardening_flags(tmp_path):
    cmd = DockerRunner("img:tag").build_command(tmp_path / "scene.py", tmp_path / "out", "l", "c1")
    joined = " ".join(cmd)
    for flag in ["--rm", "--network none", "--memory 2g", "--memory-swap 2g", "--cpus 2",
                 "--pids-limit 256", "--read-only", "--tmpfs /tmp", "--security-opt no-new-privileges",
                 "--cap-drop ALL"]:
        assert flag in joined, flag
    assert f"{(tmp_path / 'scene.py').resolve()}:/work/scene.py:ro" in cmd
    assert f"{ENTRY_SCRIPT}:/opt/stv/render_entry.py:ro" in cmd
    assert cmd[cmd.index("img:tag") + 1:] == ["python", "/opt/stv/render_entry.py", "/work/scene.py",
                                              "/out", "l", "LessonScene"]


def test_get_runner_selects_backend(env, monkeypatch):
    from core.config import get_settings
    monkeypatch.setenv("RENDER_BACKEND", "docker")
    assert isinstance(get_runner(get_settings()), DockerRunner)
    monkeypatch.setenv("RENDER_BACKEND", "local")
    assert isinstance(get_runner(get_settings()), LocalRunner)
    monkeypatch.setenv("RENDER_BACKEND", "bogus")
    with pytest.raises(ValueError):
        get_runner(get_settings())


def test_entry_script_rejects_bad_usage(tmp_path):
    import subprocess, sys
    r = subprocess.run([sys.executable, str(ENTRY_SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 2 and "usage" in r.stderr


# ------------------------------------------------- local runner (real manim)
@pytest.mark.manim
def test_local_hello_world_renders(tmp_path):
    res = LocalRunner().render(write(tmp_path, HELLO), tmp_path / "out", "l", timeout_s=120)
    assert res.success, res.stderr_tail
    assert res.exit_reason == "ok"
    assert res.video_path and res.video_path.exists() and res.video_path.suffix == ".mp4"
    assert res.duration_s > 0


@pytest.mark.manim
def test_local_name_error_is_reported_structurally(tmp_path):
    res = LocalRunner().render(write(tmp_path, NAME_ERROR), tmp_path / "out", "l", timeout_s=120)
    assert not res.success and res.exit_reason == "error"
    assert res.error_type == "NameError"
    assert "ShowCreation" in res.stderr_tail


@pytest.mark.manim
def test_local_timeout_kills_process(tmp_path):
    t0 = time.monotonic()
    res = LocalRunner().render(write(tmp_path, INFINITE), tmp_path / "out", "l", timeout_s=6)
    assert res.exit_reason == "timeout"
    assert not res.success
    assert time.monotonic() - t0 < 30


@pytest.mark.manim
def test_local_runner_scrubs_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    res = LocalRunner().render(write(tmp_path, READS_SECRET), tmp_path / "out", "l", timeout_s=120)
    assert res.success, res.stderr_tail


@pytest.mark.manim
def test_local_memory_limit(tmp_path):
    res = LocalRunner(memory_bytes=1536 * 1024**2).render(write(tmp_path, MEMORY_HOG), tmp_path / "out",
                                                          "l", timeout_s=60)
    assert res.exit_reason == "oom", (res.exit_reason, res.stderr_tail)


# ------------------------------------------------ docker runner (real boundary)
def _docker_runner():
    return DockerRunner(os.environ.get("SANDBOX_IMAGE", "stv-manim-sandbox:latest"))


@pytest.mark.docker
def test_docker_hello_world(tmp_path):
    res = _docker_runner().render(write(tmp_path, HELLO), tmp_path / "out", "l", timeout_s=180)
    assert res.success, res.stderr_tail
    assert res.video_path.exists()
    assert str(res.video_path).startswith(str((tmp_path / "out").resolve()))


@pytest.mark.docker
def test_docker_infinite_loop_times_out_and_container_is_killed(tmp_path):
    import subprocess
    t0 = time.monotonic()
    res = _docker_runner().render(write(tmp_path, INFINITE), tmp_path / "out", "l", timeout_s=10)
    assert res.exit_reason == "timeout"
    assert time.monotonic() - t0 < 60
    ps = subprocess.run(["docker", "ps", "-q", "--filter", "name=stv-render-"], capture_output=True, text=True)
    assert ps.stdout.strip() == ""


@pytest.mark.docker
def test_docker_has_no_network(tmp_path):
    res = _docker_runner().render(write(tmp_path, NETWORK), tmp_path / "out", "l", timeout_s=60)
    assert not res.success
    assert res.error_type in {"OSError", "gaierror", "TimeoutError", "ConnectionRefusedError"}
    assert "unreachable" in res.stderr_tail.lower() or "errno" in res.stderr_tail.lower()


@pytest.mark.docker
def test_docker_root_filesystem_is_read_only(tmp_path):
    res = _docker_runner().render(write(tmp_path, WRITES_ROOT), tmp_path / "out", "l", timeout_s=60)
    assert not res.success
    assert "Read-only file system" in res.stderr_tail


@pytest.mark.docker
def test_docker_pids_limit_blocks_thread_bomb(tmp_path):
    res = _docker_runner().render(write(tmp_path, MANY_THREADS), tmp_path / "out", "l", timeout_s=60)
    assert not res.success
    assert "can't start new thread" in res.stderr_tail or res.error_type == "RuntimeError"


@pytest.mark.docker
def test_docker_does_not_see_host_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    res = _docker_runner().render(write(tmp_path, READS_SECRET), tmp_path / "out", "l", timeout_s=120)
    assert res.success, res.stderr_tail


@pytest.mark.docker
def test_docker_memory_cap_reports_oom(tmp_path):
    res = DockerRunner(os.environ.get("SANDBOX_IMAGE", "stv-manim-sandbox:latest"), memory="512m").render(
        write(tmp_path, MEMORY_HOG), tmp_path / "out", "l", timeout_s=120)
    assert res.exit_reason == "oom", (res.exit_reason, res.returncode, res.stderr_tail)
