"""Static (AST) gate that runs on LLM-written Manim code BEFORE any render.

What it is for:
  * reject obviously dangerous or out-of-contract code cheaply (no container
    start, no render time burned), with a precise message the repair prompt can
    feed back to the model;
  * shrink the attack surface (no imports beyond manim/numpy/math, no dynamic
    code execution, no reflection, no dunder access, no file/asset loading).

What it is NOT: a sandbox. Python is too dynamic for a deny-list to be complete,
so the Docker container (no network, read-only FS, resource caps, no caps) is
the real security boundary. See README "Security model".
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field

MAX_LINES = 400
SCENE_NAME = "LessonScene"
ALLOWED_SCENE_BASES = {"Scene", "MovingCameraScene"}

# `from manim import *` (or named imports from manim), numpy, math.
ALLOWED_IMPORT_MODULES = {"numpy", "math"}
ALLOWED_FROM_MODULES = {"manim", "numpy", "math"}

BANNED_CALL_NAMES = {
    "eval", "exec", "compile", "open", "__import__", "getattr", "setattr", "delattr", "globals",
    "locals", "vars", "input", "breakpoint", "exit", "quit", "help", "memoryview",
}
# `from manim import *` leaks ~580 names into the scene's namespace. Some are
# module objects that lead (via attribute chains) to os/subprocess, and some are
# helpers that touch the filesystem or spawn processes, e.g. `capture()` is a
# subprocess.Popen wrapper and `guarantee_empty_existence()` calls rmtree.
# `config`/`tempconfig` are banned so render settings stay host-controlled.
MANIM_LEAKED_MODULES = {"utils", "plugins", "gui", "opengl", "core", "renderer", "typing"}
MANIM_DANGEROUS_HELPERS = {
    "capture", "open_file", "open_media_file", "ensure_executable", "guarantee_existence",
    "guarantee_empty_existence", "get_dir_layout", "get_video_metadata", "modify_atime",
    "write_to_movie", "get_full_sound_file_path", "seek_full_path_from_defaults", "get_plugins",
    "list_plugins", "add_extension_if_not_present", "config", "tempconfig",
}
BANNED_NAMES = BANNED_CALL_NAMES | MANIM_LEAKED_MODULES | MANIM_DANGEROUS_HELPERS
# Manim/numpy entry points that read files or assets from disk.
BANNED_CONSTRUCTORS = {"ImageMobject", "SVGMobject", "Code", "ThreeDScene", "SpecialThreeDScene"}
# Attribute names that are either dangerous modules reachable through chains
# (manim.utils.commands.subprocess, ...) or file/process/env access.
BANNED_ATTRIBUTES = {
    "os", "sys", "subprocess", "shutil", "socket", "pathlib", "Path", "importlib", "builtins",
    "ctypes", "ctypeslib", "io", "tempfile", "pickle", "marshal", "shelve", "urllib", "request",
    "http", "multiprocessing", "threading", "signal", "resource", "glob", "popen", "system", "spawn",
    "fork", "environ", "open", "remove", "unlink", "rmdir", "rmtree", "listdir", "scandir", "walk",
    "chdir", "load", "save", "savez", "savez_compressed", "savetxt", "loadtxt", "fromfile", "tofile",
    "genfromtxt", "memmap", "f2py", "lib", "add_sound", "urlopen", "write_text", "read_text",
    "write_bytes", "read_bytes", "exec_module", "loader", "eval", "exec", "compile", "getattr",
    "setattr", "delattr",
}
ALLOWED_TOP_LEVEL = (ast.Import, ast.ImportFrom, ast.ClassDef, ast.FunctionDef, ast.Assign,
                     ast.AnnAssign)


@dataclass
class Violation:
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"line {self.line}: [{self.rule}] {self.message}"


@dataclass
class ValidationResult:
    ok: bool
    violations: list[Violation] = field(default_factory=list)
    syntax_error: bool = False

    @property
    def error_class(self) -> str | None:
        if self.ok:
            return None
        return "syntax" if self.syntax_error else "validation"

    def format(self) -> str:
        return "\n".join(str(v) for v in self.violations)


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__") and len(name) > 4


class _Checker(ast.NodeVisitor):
    def __init__(self) -> None:
        self.v: list[Violation] = []

    def add(self, node: ast.AST, rule: str, msg: str) -> None:
        self.v.append(Violation(getattr(node, "lineno", 0), rule, msg))

    # imports ---------------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name not in ALLOWED_IMPORT_MODULES:
                self.add(node, "import", f"import of '{alias.name}' is not allowed "
                         "(only `from manim import *`, numpy and math)")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level or node.module not in ALLOWED_FROM_MODULES:
            self.add(node, "import", f"`from {'.' * node.level}{node.module or ''} import ...` is not "
                     "allowed (only `from manim import *`, numpy and math)")
        self.generic_visit(node)

    # names / calls ---------------------------------------------------------
    def visit_Name(self, node: ast.Name) -> None:
        if node.id in BANNED_NAMES:
            self.add(node, "banned-name", f"use of '{node.id}' is not allowed")
        elif _is_dunder(node.id):
            self.add(node, "dunder", f"dunder name '{node.id}' is not allowed")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if _is_dunder(node.attr):
            self.add(node, "dunder", f"dunder attribute access '.{node.attr}' is not allowed")
        elif node.attr in BANNED_ATTRIBUTES:
            self.add(node, "banned-attribute", f"attribute '.{node.attr}' is not allowed")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in BANNED_CONSTRUCTORS:
            self.add(node, "asset", f"'{node.func.id}' is not allowed (no external files/assets, no 3D)")
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id in BANNED_CONSTRUCTORS:
                self.add(node, "asset", f"subclassing '{base.id}' is not allowed")
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        self.add(node, "global", "`global` statements are not allowed")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.add(node, "async", "async code is not allowed")


def _scene_classes(tree: ast.Module) -> list[ast.ClassDef]:
    out = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = {b.id for b in node.bases if isinstance(b, ast.Name)}
            if bases & (ALLOWED_SCENE_BASES | {"ThreeDScene", "ZoomedScene", "SpecialThreeDScene"}) or \
                    node.name.endswith("Scene"):
                out.append(node)
    return out


def validate_code(code: str) -> ValidationResult:
    lines = code.splitlines()
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return ValidationResult(False, [Violation(exc.lineno or 0, "syntax",
                                                  f"{type(exc).__name__}: {exc.msg}")], syntax_error=True)
    violations: list[Violation] = []
    if len(lines) > MAX_LINES:
        violations.append(Violation(MAX_LINES, "length", f"code is {len(lines)} lines; max is {MAX_LINES}"))

    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue  # docstring
        if not isinstance(node, ALLOWED_TOP_LEVEL):
            violations.append(Violation(node.lineno, "top-level", f"top-level `{type(node).__name__}` "
                                        "statement is not allowed; put logic inside construct()"))

    checker = _Checker()
    checker.visit(tree)
    violations.extend(checker.v)

    scenes = _scene_classes(tree)
    if len(scenes) != 1:
        violations.append(Violation(0, "scene", f"expected exactly one Scene subclass, found {len(scenes)}"))
    else:
        cls = scenes[0]
        bases = {b.id for b in cls.bases if isinstance(b, ast.Name)}
        if cls.name != SCENE_NAME:
            violations.append(Violation(cls.lineno, "scene", f"the scene class must be named {SCENE_NAME}"))
        if not bases & ALLOWED_SCENE_BASES:
            violations.append(Violation(cls.lineno, "scene", f"{cls.name} must subclass Scene"))
        if not any(isinstance(n, ast.FunctionDef) and n.name == "construct" for n in cls.body):
            violations.append(Violation(cls.lineno, "scene", f"{cls.name} must define construct(self)"))

    violations.sort(key=lambda v: v.line)
    return ValidationResult(not violations, violations)
