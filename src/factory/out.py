"""How a command prints its result (PRD FR-20 to FR-24): TOON by default, JSON on request.

TOON (Token-Optimized Object Notation) is YAML-like text that names the keys of a uniform list once:

    total: 2
    runbooks[2]{name,traits}:
      research,review
      app-build,frontend backend

This module has no dependency. It encodes the subset the factory prints: objects, lists, and scalars.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .config import FactoryError

CLIP = 200  # characters of long text shown before the note that says how to see the rest
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*$")
_NUMBER = re.compile(r"-?\d+(\.\d+)?([eE][+-]?\d+)?$")


class UsageError(FactoryError):
    """The command line is wrong: a flag is missing, unknown or does not fit. Exit code 2."""


def _number(v: float) -> str:
    return f"{v:.6f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def _string(s: str, delimiter: str = ",") -> str:
    plain = (s and s == s.strip() and s not in ("true", "false", "null") and not _NUMBER.match(s)
             and not s.startswith("-") and not any(c in s for c in f'{delimiter}:"\\[]{{}}\n\r\t#'))
    if plain:
        return s
    escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{escaped}"'


def _scalar(v: Any, delimiter: str = ",") -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return _number(v)
    return _string(str(v), delimiter)


def _key(k: str) -> str:
    return k if _KEY.match(k) else _string(k)


def _is_scalar(v: Any) -> bool:
    return not isinstance(v, (dict, list, tuple))


def _lines(key: str | None, v: Any) -> list[str]:
    """The lines for one value, under `key` (None at the root)."""
    head = _key(key) if key is not None else ""
    if isinstance(v, dict):
        body = [line for k, x in v.items() for line in _lines(k, x)]
        if key is None:
            return body
        return [f"{head}:", *("  " + line for line in body)]
    if isinstance(v, (list, tuple)):
        n = len(v)
        if not n:
            return [f"{head}[0]:"]
        if key == "help":  # command templates: one for each line, so a space or a flag needs no quotes
            return [f"{head}[{n}]:", *(f"  - {x}" for x in v)]
        if all(_is_scalar(x) for x in v):
            return [f"{head}[{n}]: " + ",".join(_scalar(x) for x in v)]
        fields = list(v[0]) if isinstance(v[0], dict) else None
        if fields and all(isinstance(x, dict) and list(x) == fields and all(_is_scalar(y) for y in x.values())
                          for x in v):
            return [f"{head}[{n}]{{{','.join(_key(f) for f in fields)}}}:",
                    *("  " + ",".join(_scalar(x[f]) for f in fields) for x in v)]
        out = [f"{head}[{n}]:"]
        for x in v:
            inner = _lines(None, x) if not _is_scalar(x) else [_scalar(x)]
            out += ["  - " + inner[0], *("    " + line for line in inner[1:])]
        return out
    return [f"{head}: {_scalar(v)}"]


def toon(data: Any) -> str:
    """Encode `data` (dicts, lists and scalars) as TOON."""
    return "\n".join(_lines(None, data))


def clip(text: str, full: bool = False, limit: int = CLIP) -> str:
    """Cut long text and say how long it is and how to see all of it (PRD FR-22)."""
    text = " ".join(str(text).split())
    if full or len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}... (truncated, {len(text)} chars in all; use --full)"


def choose(rows: list[dict], default: list[str], extra: list[str], asked: str | None) -> list[dict]:
    """Keep the default fields of each row, plus the extra fields that `--fields` names (PRD FR-21)."""
    wanted = [f.strip() for f in asked.split(",") if f.strip()] if asked else []
    unknown = [f for f in wanted if f not in default and f not in extra]
    if unknown:
        raise UsageError(f"--fields: no field {', '.join(unknown)}; the fields are {', '.join(default + extra)}")
    keep = default + [f for f in wanted if f not in default]
    return [{f: r.get(f) for f in keep} for r in rows]


def add_flags(parser, *, fields: bool = False, full: bool = False) -> None:
    """The output flags that a command takes: --json always, --fields and --full when it has them."""
    parser.add_argument("--json", action="store_true", help="print JSON instead of TOON")
    if fields:
        parser.add_argument("--fields", metavar="A,B", help="add these fields to each row (default: a short list)")
    if full:
        parser.add_argument("--full", action="store_true", help="do not cut long text")


def error(message: str, as_json: bool = False) -> str:
    """A failure as data on stdout (PRD FR-25). Stderr is for logs."""
    return json.dumps({"error": message}, indent=2) if as_json else toon({"error": message})


def show(data: Any, args, *, plain: Any = None, help: list[str] | None = None) -> str:
    """The text to print: JSON when --json is set (`plain` if given, else `data`), otherwise TOON.
    `help` is a list of command templates for the likely next steps; it ends the output (PRD FR-28)."""
    tail = {"help": help} if help else {}
    if getattr(args, "json", False):
        base = data if plain is None else plain
        return json.dumps({**base, **tail}, indent=2)
    return toon({**data, **tail})
