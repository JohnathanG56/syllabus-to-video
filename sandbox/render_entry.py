"""Entry point that runs INSIDE the sandbox (Docker container, or the unsafe local
subprocess). It renders one scene file with Manim and writes a machine-readable
``result.json`` next to the output so the host never has to parse Manim's rich
console output to know what happened.

Usage: python render_entry.py SCENE_FILE OUT_DIR QUALITY [SCENE_NAME]

Exit code 0 = rendered, 1 = scene raised, 2 = bad invocation.
This file must only use the standard library + manim (it is mounted read-only
into the container).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import traceback

QUALITY = {
    "l": "low_quality",
    "m": "medium_quality",
    "h": "high_quality",
    "p": "production_quality",
    "k": "fourk_quality",
}


def _find_scene_class(module, scene_name, Scene):
    cls = getattr(module, scene_name, None)
    if isinstance(cls, type) and issubclass(cls, Scene):
        return cls
    candidates = [
        v for v in vars(module).values()
        if isinstance(v, type) and issubclass(v, Scene) and v is not Scene
        and v.__module__ == module.__name__
    ]
    if len(candidates) == 1:
        return candidates[0]
    raise LookupError(f"expected a Scene subclass named {scene_name!r}; found {[c.__name__ for c in candidates]}")


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[3] not in QUALITY:
        print("usage: render_entry.py SCENE_FILE OUT_DIR QUALITY(l|m|h|p|k) [SCENE_NAME]", file=sys.stderr)
        return 2
    scene_path, out_dir, quality = argv[1], argv[2], argv[3]
    scene_name = argv[4] if len(argv) > 4 else "LessonScene"
    result: dict = {"ok": False, "video": None, "error_type": None, "error_message": None}
    started = time.monotonic()
    try:
        from manim import Scene, tempconfig

        opts = {
            "quality": QUALITY[quality],
            "media_dir": out_dir,
            "input_file": scene_path,
            "output_file": "render",
            "disable_caching": True,
            "progress_bar": "none",
            "verbosity": "WARNING",
            "write_to_movie": True,
            "format": "mp4",
        }
        with tempconfig(opts):
            spec = importlib.util.spec_from_file_location("lesson_scene", scene_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            scene_cls = _find_scene_class(module, scene_name, Scene)
            scene = scene_cls()
            scene.render()
            result["video"] = str(scene.renderer.file_writer.movie_file_path)
            result["ok"] = True
    except BaseException as exc:  # noqa: BLE001 - we report everything, incl. SystemExit
        result["error_type"] = type(exc).__name__
        result["error_message"] = str(exc)[:4000]
        traceback.print_exc(file=sys.stderr)
    result["render_seconds"] = round(time.monotonic() - started, 3)
    try:
        with open(os.path.join(out_dir, "result.json"), "w") as fh:
            json.dump(result, fh)
    except OSError as exc:
        print(f"could not write result.json: {exc}", file=sys.stderr)
    sys.stderr.flush()
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
