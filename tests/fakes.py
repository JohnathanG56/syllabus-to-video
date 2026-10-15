"""Canned LLM payloads shared by tests."""
import json

TOPICS = {
    "course_title": "MATH 33A: Linear Algebra",
    "topics": [
        {"title": "Vectors as arrows",
         "objectives": ["Draw a vector as an arrow from the origin", "Add vectors tip to tail",
                        "Scale a vector by a number"],
         "prerequisites": []},
        {"title": "Linear transformations",
         "objectives": ["Describe a transformation by where basis vectors land",
                        "Recognize rotations and shears", "Compute a matrix-vector product"],
         "prerequisites": ["Vectors as arrows"]},
        {"title": "The determinant",
         "objectives": ["Interpret the determinant as area scaling", "Compute a 2x2 determinant",
                        "Explain what a zero determinant means"],
         "prerequisites": ["Linear transformations"]},
    ],
}

_NARR = ("A vector is an arrow that starts at the origin and points somewhere in the plane. "
         "Its two coordinates tell you how far to walk along x and then along y to reach the tip.")


def script(n_segments: int = 4, words_per_segment: int = 45) -> dict:
    base = (_NARR + " ") * 4
    words = base.split()
    segs = []
    for i in range(n_segments):
        segs.append({
            "narration": " ".join(words[:words_per_segment]),
            "visual": f"Segment {i}: draw axes and an arrow from the origin to (2, 1).",
            "key_equation": "\\vec{v} = (2, 1)" if i == 1 else None,
        })
    return {"title": "Vectors as arrows", "segments": segs}


def dumps(obj) -> str:
    return json.dumps(obj)


GOOD_SCENE = '''from manim import *


class LessonScene(Scene):
    def sync_to(self, i):
        remaining = SEGMENT_ENDS[i] - self.renderer.time
        if remaining > 0.02:
            self.wait(remaining)

    def construct(self):
{body}
'''


def good_scene(n_segments: int, extra: str = "") -> str:
    body = "\n".join(f"        self.play(Create(Circle()), run_time=0.5){extra}\n        self.sync_to({i})"
                     for i in range(n_segments))
    return GOOD_SCENE.format(body=body)


class FfmpegFakeRunner:
    """Stands in for the sandbox in pipeline tests: 'renders' a solid-color MP4
    whose length is the scene's injected SEGMENT_ENDS[-1]; fails on ShowCreation."""

    name = "fake"

    def __init__(self, fail_final: bool = False):
        self.calls = []
        self.fail_final = fail_final

    def render(self, scene_file, out_dir, quality="l", timeout_s=180, scene_name="LessonScene"):
        import re
        import subprocess
        from pathlib import Path
        from sandbox.run import RenderResult
        code = Path(scene_file).read_text()
        self.calls.append((quality, code))
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        if "ShowCreation" in code:
            return RenderResult(False, None, "Traceback (most recent call last):\n  File \"/work/scene.py\", "
                                "line 20, in construct\nNameError: name 'ShowCreation' is not defined", 0.5,
                                "error", "NameError", "name 'ShowCreation' is not defined", 1)
        if self.fail_final and quality != "l":
            return RenderResult(False, None, "boom", 0.1, "timeout", "Timeout", None, -9)
        m = re.search(r"SEGMENT_ENDS = \[([^\]]*)\]", code)
        seconds = float(m.group(1).split(",")[-1]) if m else 2.0
        video = out_dir / "render.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"color=c=navy:size=320x180:rate=15:duration={seconds}", "-pix_fmt", "yuv420p",
                        str(video)], check=True)
        return RenderResult(True, video, "", 0.3, "ok", None, None, 0)
