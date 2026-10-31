"""Agent roles: the kinds of agent a runbook can staff a step with.

A role says what an agent is for, what it may do and how it behaves, whatever the task. A
runbook step gives the task. agents.yaml (or the runbook) says which worker and model play
each role, so the same role can run on Claude in one runbook and on Codex in another.

The roles are data: roles.yaml in the repository root, or the file named by the FACTORY_ROLES
environment variable. The agents the fleet launches inherit that variable, so they read the same file.

`runs` names the commands the role may run. `test` and `build` come from the scaffold's
templates/<name>.yaml; `search` (the factory's own web search) and `curl` are always available.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import ConfigError
from .invoke import Tools
from .paths import ROLES_FILE

SEARCH = f"{sys.executable} -m factory.search"
COMMON_COMMANDS = {"search": SEARCH, "curl": "curl"}
NETWORK_COMMANDS = {"search", "curl"}
ROLES_ENV = "FACTORY_ROLES"
NAME = r"[a-z][a-z0-9_]*"
KNOWN_COMMANDS = {"test", "build", *COMMON_COMMANDS}


@dataclass(frozen=True)
class Role:
    name: str
    title: str
    description: str
    prompt: str
    edit: bool = False
    web: bool = False
    runs: tuple[str, ...] = ()
    asks: bool = False  # may pause its task to ask the user questions

    def tools(self, commands: dict[str, str]) -> Tools:
        """The tools for this role in a directory whose commands are `commands`."""
        available = {**COMMON_COMMANDS, **commands}
        runs = [r for r in self.runs if r in available]
        return Tools(edit=self.edit, web=self.web, bash=tuple(dict.fromkeys(available[r] for r in runs)),
                     network=bool(NETWORK_COMMANDS & set(runs)))


class _RoleDef(BaseModel):
    """One role as written in roles.yaml."""

    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    edit: bool = False
    web: bool = False
    runs: list[str] = []
    asks: bool = False


def load_roles(path: Path | None = None) -> dict[str, Role]:
    """Read the roles from `path` (default: $FACTORY_ROLES, else roles.yaml in the repository)."""
    path = path or Path(os.environ.get(ROLES_ENV) or ROLES_FILE)
    try:
        raw = yaml.safe_load(path.read_text())
    except OSError as e:
        raise ConfigError(f"cannot read the roles file {path}: {e.strerror}") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"{path.name}: not valid YAML: {e}") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("roles"), dict) or not raw["roles"]:
        raise ConfigError(f"{path.name}: expected a `roles:` mapping of role name to definition")
    unknown_keys = set(raw) - {"roles"}
    if unknown_keys:
        raise ConfigError(f"{path.name}: unknown top-level keys {', '.join(sorted(map(str, unknown_keys)))}")
    roles = {}
    for name, body in raw["roles"].items():
        if not isinstance(name, str) or not re.fullmatch(NAME, name):
            raise ConfigError(f"{path.name}: role name {name!r} must be lowercase letters, digits and _")
        try:
            d = _RoleDef.model_validate(body)
        except ValidationError as e:
            x = e.errors()[0]
            where = ".".join(map(str, x["loc"])) or "<role>"
            raise ConfigError(f"{path.name}: role {name!r}, {where}: {x['msg']}") from None
        bad = [r for r in d.runs if r not in KNOWN_COMMANDS]
        if bad:
            raise ConfigError(f"{path.name}: role {name!r} runs {', '.join(bad)}; "
                              f"commands are {', '.join(sorted(KNOWN_COMMANDS))}")
        roles[name] = Role(name, d.title, d.description, d.prompt.replace("{search}", SEARCH),
                           edit=d.edit, web=d.web, runs=tuple(d.runs), asks=d.asks)
    return roles


class _LazyRoles(Mapping):
    """The roles, read from the file on first use, so a bad file is reported by the command that needs it."""

    def __init__(self) -> None:
        self._roles: dict[str, Role] | None = None

    def _load(self) -> dict[str, Role]:
        if self._roles is None:
            self._roles = load_roles()
        return self._roles

    def __getitem__(self, name: str) -> Role:
        return self._load()[name]

    def __iter__(self):
        return iter(self._load())

    def __len__(self) -> int:
        return len(self._load())


ROLES: Mapping[str, Role] = _LazyRoles()
