"""LLM configuration: endpoints, keys, allowed models, reviewer and budgets.

`llm.yaml` is the single source of truth. Secrets never appear in it: a worker
names the environment variable that holds its key (`api_key_env`), and `${VAR}`
placeholders in other fields are resolved from the environment at load time.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

WireApi = Literal["anthropic-messages", "responses"]

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WORKER_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SECRET_KEYS = {"api_key", "apikey", "token", "secret", "password", "authorization"}


class FactoryError(Exception):
    """Base class for configuration and policy errors."""


class ConfigError(FactoryError):
    """The config file is invalid."""


class ModelNotAllowed(FactoryError):
    """A request named a model or worker the config does not permit."""


def _unresolved(value: str) -> bool:
    return "${" in value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMDefaults(_Strict):
    """Global defaults; each worker may override base_url, api_key_env and wire_api."""

    base_url: str
    api_key_env: str
    wire_api: WireApi
    allowed_models: list[str] = Field(min_length=1)
    default_model: str

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str) -> str:
        return _check_url(v)

    @field_validator("api_key_env")
    @classmethod
    def _env_name(cls, v: str) -> str:
        return _check_env_name(v)

    @field_validator("allowed_models")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("duplicate entries")
        return v


class WorkerSpec(_Strict):
    enabled: bool = True
    harness: str
    base_url: str | None = None
    api_key_env: str | None = None
    wire_api: WireApi | None = None
    models: list[str] = Field(min_length=1)
    default_model: str | None = None

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return None if v is None else _check_url(v)

    @field_validator("api_key_env")
    @classmethod
    def _env_name(cls, v: str | None) -> str | None:
        return None if v is None else _check_env_name(v)


class ReviewerSpec(_Strict):
    candidates: list[str] = Field(min_length=1)
    must_differ_from_author: bool = True


class Budgets(_Strict):
    per_task_usd: float = Field(gt=0)
    per_day_usd: float | None = Field(default=None, gt=0)
    max_retries: int = Field(default=1, ge=0)


class Worker(BaseModel):
    """A worker with global defaults applied."""

    model_config = ConfigDict(frozen=True)

    name: str
    enabled: bool
    harness: str
    base_url: str
    api_key_env: str
    wire_api: WireApi
    models: tuple[str, ...]
    default_model: str


class Config(_Strict):
    version: Literal[1]
    llm: LLMDefaults
    workers: dict[str, WorkerSpec] = Field(min_length=1)
    reviewer: ReviewerSpec
    budgets: Budgets

    @model_validator(mode="after")
    def _cross_checks(self) -> Config:
        allowed = set(self.llm.allowed_models)

        def require_allowed(model: str, where: str) -> None:
            # Entries of disabled workers may still hold an unresolved ${VAR}.
            if not _unresolved(model) and model not in allowed:
                raise ValueError(f"{where}: model {model!r} is not in llm.allowed_models")

        if self.llm.default_model not in allowed:
            raise ValueError(
                f"llm.default_model {self.llm.default_model!r} is not in llm.allowed_models"
            )

        for name, w in self.workers.items():
            if not _WORKER_NAME.match(name):
                raise ValueError(f"workers.{name}: name must be lowercase letters, digits, '-'")
            if len(set(w.models)) != len(w.models):
                raise ValueError(f"workers.{name}.models: duplicate entries")
            for m in w.models:
                require_allowed(m, f"workers.{name}.models")
            if w.default_model is not None and w.default_model not in w.models:
                raise ValueError(
                    f"workers.{name}.default_model {w.default_model!r} is not in its models"
                )

        for m in self.reviewer.candidates:
            require_allowed(m, "reviewer.candidates")

        enabled = self.enabled_workers()
        if not enabled:
            raise ValueError("workers: at least one worker must be enabled")
        if self.reviewer.must_differ_from_author:
            reviewable = {m for w in enabled for m in w.models if m in self.reviewer.candidates}
            if len(reviewable) < 2:
                raise ValueError(
                    "reviewer: must_differ_from_author needs at least two reviewer candidates "
                    "served by enabled workers"
                )
        return self

    def worker(self, name: str) -> Worker:
        spec = self.workers.get(name)
        if spec is None:
            raise ConfigError(f"unknown worker {name!r}")
        return Worker(
            name=name,
            enabled=spec.enabled,
            harness=spec.harness,
            base_url=spec.base_url or self.llm.base_url,
            api_key_env=spec.api_key_env or self.llm.api_key_env,
            wire_api=spec.wire_api or self.llm.wire_api,
            models=tuple(spec.models),
            default_model=spec.default_model or spec.models[0],
        )

    def enabled_workers(self) -> list[Worker]:
        return [self.worker(n) for n, s in self.workers.items() if s.enabled]

    def check_request(self, worker_name: str, model: str | None = None) -> str:
        """Return the model to use, or raise ModelNotAllowed.

        `model=None` selects the worker's default. Rejects disabled workers and
        any model outside the global allowlist or the worker's own list.
        """
        if worker_name not in self.workers:
            raise ModelNotAllowed(f"unknown worker {worker_name!r}")
        w = self.worker(worker_name)
        if not w.enabled:
            raise ModelNotAllowed(f"worker {worker_name!r} is disabled")
        chosen = model or w.default_model
        if chosen not in self.llm.allowed_models:
            raise ModelNotAllowed(f"model {chosen!r} is not in llm.allowed_models")
        if chosen not in w.models:
            raise ModelNotAllowed(f"model {chosen!r} is not allowed for worker {worker_name!r}")
        return chosen

    def reviewer_for(self, author_model: str) -> str:
        """Pick the reviewer model for a PR written with `author_model` (gate 5).

        Takes the first candidate served by an enabled worker that differs from
        the author's model.
        """
        served = {m for w in self.enabled_workers() for m in w.models}
        for m in self.reviewer.candidates:
            if m not in served:
                continue
            if self.reviewer.must_differ_from_author and m == author_model:
                continue
            return m
        raise ConfigError(f"no reviewer model available for author model {author_model!r}")


def _check_url(v: str) -> str:
    if _unresolved(v):
        return v
    parsed = urlparse(v)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"not a valid http(s) URL: {v!r}")
    return v


def _check_env_name(v: str) -> str:
    if not _ENV_NAME.match(v):
        raise ValueError(
            "must be the NAME of an environment variable, not a key; "
            "literal credentials are not allowed"
        )
    return v


def _interpolate(node: Any, env: Mapping[str, str], missing: set[str], strict: bool) -> Any:
    if isinstance(node, str):

        def sub(m: re.Match[str]) -> str:
            value = env.get(m.group(1))
            if value:
                return value
            if strict:
                missing.add(m.group(1))
            return m.group(0)

        return _VAR.sub(sub, node)
    if isinstance(node, list):
        return [_interpolate(n, env, missing, strict) for n in node]
    if isinstance(node, dict):
        return {k: _interpolate(v, env, missing, strict) for k, v in node.items()}
    return node


def _find_literal_secrets(node: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            here = f"{path}.{k}" if path else str(k)
            if str(k).lower() in _SECRET_KEYS:
                found.append(here)
            found += _find_literal_secrets(v, here)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found += _find_literal_secrets(v, f"{path}[{i}]")
    return found


def _format_errors(err: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg'].removeprefix('Value error, ')}"
        for e in err.errors()
    )


def load_config(path: str | Path, env: Mapping[str, str] | None = None) -> Config:
    """Load and validate `llm.yaml`.

    `${VAR}` placeholders resolve from `env` (default `os.environ`); an unset or
    empty variable is a load error, except inside a disabled worker. Every
    enabled worker's `api_key_env` must name a variable that is set.
    """
    env = os.environ if env is None else env
    try:
        raw = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    secrets = _find_literal_secrets(raw)
    if secrets:
        raise ConfigError(
            f"literal credentials are not allowed ({', '.join(secrets)}); "
            "set api_key_env to the name of an environment variable"
        )

    missing: set[str] = set()
    resolved: dict[str, Any] = {}
    for key, value in raw.items():
        if key == "workers" and isinstance(value, dict):
            resolved[key] = {
                name: _interpolate(
                    spec,
                    env,
                    missing,
                    strict=not (isinstance(spec, dict) and spec.get("enabled", True) is False),
                )
                for name, spec in value.items()
            }
        else:
            resolved[key] = _interpolate(value, env, missing, strict=True)
    if missing:
        raise ConfigError(f"environment variables not set: {', '.join(sorted(missing))}")

    try:
        cfg = Config.model_validate(resolved)
    except ValidationError as e:
        raise ConfigError(_format_errors(e)) from e

    unset = sorted(
        {
            f"{w.api_key_env} (worker {w.name})"
            for w in cfg.enabled_workers()
            if not env.get(w.api_key_env)
        }
    )
    if unset:
        raise ConfigError(f"API key environment variables not set: {', '.join(unset)}")
    return cfg
