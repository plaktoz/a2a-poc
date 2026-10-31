"""Scaffolds: templates copied into a run's workspace, one directory per surface.

Each template `templates/<name>/` has a `templates/<name>.yaml` beside it that tells the factory
how to work with it, so runbooks never hard-code paths or commands:

  surface:     what it is, for people ("frontend", "backend")
  stack:       text every agent working in it is given (fixed; agents may not change it)
  suite:       vitest | pytest       how the orchestrator runs and reads its tests
  serve:       vite | asgi | null    how the `serves` gate starts it
  commands:    {test: ..., build: ...}  commands agents may run in it
  test_files:  globs the tester writes
  source_files: globs the developer writes
  protected:   files no agent may change (checked to exist after install)

Globs are relative to the scaffold directory and match with fnmatch, where `*` also matches `/`.
"""

from __future__ import annotations

import fnmatch
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import ConfigError
from .paths import TEMPLATES_DIR
from .process import run, tail

KEEP = {"node_modules", ".venv"}  # installed, not generated: survive a reset
IGNORED = {*KEEP, ".git", "dist", ".factory", "__pycache__", ".pytest_cache"}


@dataclass(frozen=True)
class Template:
    name: str
    path: Path
    surface: str
    stack: str
    suite: str
    serve: str | None
    commands: dict[str, str]
    test_files: tuple[str, ...]
    source_files: tuple[str, ...]
    protected: tuple[str, ...]


def read_template(name: str, templates_dir: Path = TEMPLATES_DIR) -> Template:
    path, meta_file = templates_dir / name, templates_dir / f"{name}.yaml"
    if not path.is_dir() or not meta_file.is_file():
        raise ConfigError(f"template {name!r} needs {path}/ and {meta_file.name} beside it")
    m = yaml.safe_load(meta_file.read_text()) or {}
    try:
        tpl = Template(
            name=name, path=path, surface=m["surface"], stack=str(m["stack"]).strip(),
            suite=m["suite"], serve=m.get("serve"), commands=dict(m.get("commands", {})),
            test_files=tuple(m["test_files"]), source_files=tuple(m["source_files"]),
            protected=tuple(m.get("protected", [])),
        )
    except KeyError as e:
        raise ConfigError(f"template {name!r}: {meta_file.name} has no {e.args[0]!r}") from None
    if tpl.suite not in ("vitest", "pytest") or tpl.serve not in (None, "vite", "asgi"):
        raise ConfigError(f"template {name!r}: unknown suite {tpl.suite!r} or serve {tpl.serve!r}")
    return tpl


def matches(rel: str, globs: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(rel, g) for g in globs)


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")
            if p.is_file() and not IGNORED & set(p.relative_to(root).parts)}


def install(dest: Path) -> None:
    if (dest / "package.json").exists() and not (dest / "node_modules").exists():
        r = run(["npm", "install"], cwd=dest)
        if r.returncode != 0:
            raise RuntimeError(f"npm install in {dest.name} failed:\n{tail(r)}")
    if (dest / "pyproject.toml").exists() and not (dest / ".venv").exists():
        r = run(["uv", "sync", "--quiet"], cwd=dest)
        if r.returncode != 0:
            raise RuntimeError(f"uv sync in {dest.name} failed:\n{tail(r)}")


def place(app_dir: Path, dir_name: str, tpl: Template, *, install_deps: bool = True) -> None:
    """Copy the template into app_dir/dir_name unless it is there, then install it."""
    dest = app_dir / dir_name
    if not dest.exists():
        shutil.copytree(tpl.path, dest, ignore=shutil.ignore_patterns(*IGNORED))
    if install_deps:
        install(dest)
        if missing := [p for p in tpl.protected if not (dest / p).exists()]:
            raise ConfigError(f"{dest} is missing template files: {', '.join(missing)}")


def reset(app_dir: Path, dir_name: str, tpl: Template) -> None:
    """Return a scaffold to its template: remove generated files, restore template ones.

    Installed dependencies and lock files written by the install are kept.
    """
    dest = app_dir / dir_name
    template_files = _files(tpl.path)
    for rel in _files(dest) - template_files - set(tpl.protected):
        (dest / rel).unlink()
    for rel in template_files:
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tpl.path / rel, dest / rel)
    for d in sorted((p for p in dest.rglob("*") if p.is_dir() and not IGNORED & set(p.parts)), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()
