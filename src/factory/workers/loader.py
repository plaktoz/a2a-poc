"""Load worker plugins by module path (importlib; not a package entry point).

`load_config` calls `validate_workers` once, so a bad plugin path or an interface
that does not match `base.py` fails when llm.yaml loads, not on the first call.
"""

from __future__ import annotations

import importlib
import inspect
import re
from collections.abc import Callable
from typing import Any

from ..config import Config, ConfigError
from .base import WorkerPlugin

_MODULE_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def _accepts(fn: Callable[..., Any] | None, n_args: int) -> bool:
    if fn is None:
        return False
    try:
        inspect.signature(fn).bind(*([None] * n_args))
    except (TypeError, ValueError):
        return False
    return True


def plugin_class(path: str, worker: str = "?") -> type[WorkerPlugin]:
    """The `Plugin` class of the module at `path`. ConfigError if it cannot be imported or
    does not satisfy the interface in `base.py`."""
    where = f"worker {worker!r}: plugin {path!r}"
    if not _MODULE_PATH.match(path):
        raise ConfigError(f"{where} is not a module path, such as factory.workers.codex")
    try:
        module = importlib.import_module(path)
    except Exception as e:  # a missing module, or one that fails while it loads
        raise ConfigError(f"{where} cannot be imported: {type(e).__name__}: {e}") from e
    cls = getattr(module, "Plugin", None)
    if not inspect.isclass(cls):
        raise ConfigError(f"{where} does not define a class named Plugin")
    name = getattr(cls, "name", None)
    if not isinstance(name, str) or not name:
        raise ConfigError(f"{where}: Plugin.name must be a non-empty string")
    # Counts `self` for the plain function `run`; `validate` is a classmethod, so it takes two.
    checks = [
        ("__init__(cfg, worker)", cls, 2),
        ("run(self, prompt, cwd, tools, model)", getattr(cls, "run", None), 5),
        ("validate(cfg, worker) as a classmethod", getattr(cls, "validate", None), 2),
    ]
    missing = [label for label, fn, n in checks if not _accepts(fn, n)]
    if missing:
        raise ConfigError(
            f"{where} does not satisfy the worker plugin interface: {'; '.join(missing)} "
            "(see src/factory/workers/base.py)"
        )
    return cls


def load_plugin(cfg: Config, worker: str) -> WorkerPlugin:
    """A plugin bound to one worker of the config."""
    return plugin_class(cfg.worker(worker).plugin, worker)(cfg, worker)


def validate_workers(cfg: Config) -> None:
    """Import every worker's plugin and let it validate that worker's config. Disabled workers
    are checked too, so a typo in a module path shows up at once."""
    for name in cfg.workers:
        plugin = cfg.worker(name).plugin
        cls = plugin_class(plugin, name)
        try:
            cls.validate(cfg, name)
        except ConfigError:
            raise
        except Exception as e:
            raise ConfigError(f"worker {name!r}: plugin {plugin!r} rejected its config: {e}") from e
