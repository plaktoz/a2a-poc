"""Per-app workdirs, locking and agent ports of the sample pipeline (no LLM involved)."""

import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "sample"))

import pipeline  # noqa: E402
from factory import ConfigError  # noqa: E402
from pipeline_common import Stage  # noqa: E402


@pytest.fixture
def apps(tmp_path, monkeypatch):
    template = tmp_path / "template"
    (template / "src").mkdir(parents=True)
    (template / "package.json").write_text("{}")
    monkeypatch.setattr(pipeline, "APPS", tmp_path / "apps")
    monkeypatch.setattr(pipeline, "TEMPLATE", template)
    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    return tmp_path / "apps"


def test_new_app_is_copied_from_the_template(apps):
    workdir, lock = pipeline.open_app("todo", rebuild=False)
    assert workdir == apps / "todo" and (workdir / "package.json").exists()
    lock.close()


def test_same_app_cannot_be_built_twice_at_once(apps):
    _, lock = pipeline.open_app("todo", rebuild=False)
    with pytest.raises(ConfigError, match="already being built"):
        pipeline.open_app("todo", rebuild=True)
    lock.close()
    pipeline.open_app("todo", rebuild=True)[1].close()  # free again once the first run ends


def test_different_apps_build_at_once(apps):
    a, b = pipeline.open_app("todo", False), pipeline.open_app("blog", False)
    assert a[0] != b[0]
    a[1].close(), b[1].close()


def test_built_app_needs_rebuild(apps):
    workdir, lock = pipeline.open_app("todo", rebuild=False)
    lock.close()
    (workdir / "SPEC.json").write_text("{}")
    with pytest.raises(ConfigError, match="--rebuild"):
        pipeline.open_app("todo", rebuild=False)
    pipeline.open_app("todo", rebuild=True)[1].close()
    assert (workdir / "SPEC.json").exists()  # opening does not wipe; the run itself resets


@pytest.mark.parametrize("name", ["../x", "Todo", "a b", "", "-x", "a" * 41])
def test_bad_app_names_are_rejected(apps, name):
    with pytest.raises(ConfigError):
        pipeline.open_app(name, rebuild=False)


def test_default_app_names():
    assert pipeline.default_app_name(pipeline.DEFAULT_BRIEF) == "kanban-react"
    assert pipeline.default_app_name("A todo app, with due dates!") == "a-todo-app-with"
    assert pipeline.default_app_name("???") == "app"


def test_each_run_gets_its_own_agent_ports(tmp_path):
    stages = {n: Stage(n, "w", "m", f"http://127.0.0.1:{9100 + i}") for i, n in enumerate(["requirements", "tests"])}
    one = pipeline.Agents(stages, tmp_path / "s.yaml", True, tmp_path / "one")
    two = pipeline.Agents(stages, tmp_path / "s.yaml", True, tmp_path / "two")
    ports = [urlsplit(s.url).port for a in (one, two) for s in a.stages.values()]
    assert len(set(ports)) == 4 and not {9100, 9101} & set(ports)


def test_no_launch_keeps_the_configured_urls(tmp_path):
    stages = {"tests": Stage("tests", "w", "m", "http://127.0.0.1:9102")}
    agents = pipeline.Agents(stages, tmp_path / "s.yaml", False, tmp_path)
    assert agents.stages["tests"].url == "http://127.0.0.1:9102"
