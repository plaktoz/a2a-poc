"""The shipped templates: each one loads, holds the files its metadata names, and is a known kind of
surface for the gates. No npm, no network, no LLM."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory import gates
from factory.paths import TEMPLATES_DIR
from factory.scaffold import matches, read_template

NAMES = sorted(p.stem for p in Path(TEMPLATES_DIR).glob("*.yaml"))


def files_of(tpl):
    return {str(p.relative_to(tpl.path)) for p in tpl.path.rglob("*")
            if p.is_file() and "node_modules" not in p.parts and ".venv" not in p.parts}


def test_the_stack_templates_are_shipped():
    assert {"react-vite", "fastapi", "fastify", "bullmq-worker"} <= set(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_a_template_has_the_files_its_metadata_names(name):
    tpl = read_template(name)
    files = files_of(tpl)
    assert set(tpl.protected) <= files, f"{name}: protected files missing from the template"
    assert not any(matches(f, tpl.test_files) for f in files), f"{name}: a starter test would pass for a criterion"
    assert any(matches(f, tpl.source_files) for f in files), f"{name}: no starter source matches source_files"
    assert tpl.summary and tpl.start and tpl.stack
    assert tpl.serve is None or tpl.serve in gates.SERVERS
    assert tpl.suite in gates.SUITES


@pytest.mark.parametrize("name", [n for n in NAMES if read_template(n).suite == "vitest"])
def test_a_node_template_is_fixed_and_names_its_tests_by_criterion(name):
    tpl = read_template(name)
    files = files_of(tpl)
    assert "package-lock.json" in files  # a lock file, so an install gives the same packages
    assert "node_modules/" in (tpl.path / ".gitignore").read_text()
    assert "it('AC" in tpl.stack
    assert json.loads((tpl.path / "package.json").read_text())["scripts"]["test"] == "vitest run"


def test_the_node_templates_type_check():
    for name in ("fastify", "bullmq-worker"):
        tpl = read_template(name)
        assert tpl.commands["lint"] == "npx tsc --noEmit"
        assert "tsconfig.json" in tpl.protected


def test_the_api_template_is_served_and_the_worker_is_not():
    assert read_template("fastify").serve == "node"
    assert read_template("fastify").surface == "backend"
    assert read_template("bullmq-worker").serve is None  # it needs Redis, so the gate cannot start it


def test_the_worker_keeps_redis_out_of_the_handlers():
    tpl = read_template("bullmq-worker")
    assert "bullmq" not in (tpl.path / "src/handlers.ts").read_text()
    assert "bullmq" in (tpl.path / "src/worker.ts").read_text()
    assert "src/worker.ts" in tpl.protected


def test_the_start_command_runs_on_the_production_install():
    """A container installs with `npm ci --omit=dev`, so tsx (the start command) and every client the code imports must be
    runtime dependencies. bullmq 6 loads ioredis only when it is installed, so the worker names it itself."""
    for name in ("fastify", "bullmq-worker"):
        pkg = json.loads((read_template(name).path / "package.json").read_text())
        assert "tsx" in pkg["dependencies"], f"{name}: tsx must be a runtime dependency"
        assert "tsx" not in pkg.get("devDependencies", {}), f"{name}: tsx is still a dev dependency"
    worker = json.loads((read_template("bullmq-worker").path / "package.json").read_text())
    assert "ioredis" in worker["dependencies"]


def test_the_api_listens_on_all_interfaces_unless_told_otherwise():
    """A container reaches the API only if it listens on 0.0.0.0. server.ts is fixed, so a developer cannot change it."""
    tpl = read_template("fastify")
    text = (tpl.path / "src/server.ts").read_text()
    assert "process.env.HOST ?? '0.0.0.0'" in text and "process.env.PORT" in text
    assert "127.0.0.1" not in text and "src/server.ts" in tpl.protected


def test_the_worker_takes_its_settings_from_a_file_the_developer_may_change():
    """worker.ts is fixed, so the settings the spec may need to change live in settings.ts, which is not."""
    tpl = read_template("bullmq-worker")
    worker = (tpl.path / "src/worker.ts").read_text()
    assert "concurrency()" in worker and "queueName()" in worker and "process.env.CONCURRENCY" not in worker
    assert "src/settings.ts" not in tpl.protected and matches("src/settings.ts", tpl.source_files)
    assert "CONCURRENCY" in (tpl.path / "src/settings.ts").read_text()


# --- the node server gate, with the processes replaced ----------------------------------------


def fake_serve(monkeypatch, body, tsc_code=0):
    seen = {}
    monkeypatch.setattr(gates, "run", lambda argv, cwd: SimpleNamespace(returncode=tsc_code, stdout="bad types", stderr=""))
    monkeypatch.setattr(gates, "tail", lambda r, n=0: r.stdout)

    def serve(argv, cwd, url, check, env=None):
        seen.update(argv=argv, url=url, env=env)
        return check(body)

    monkeypatch.setattr(gates, "_serve", serve)
    return seen


def test_the_node_gate_checks_the_health_route_on_a_free_port(monkeypatch, tmp_path):
    seen = fake_serve(monkeypatch, '{"status": "ok"}')
    assert gates.serve_node(tmp_path) is None
    assert seen["url"].endswith("/api/health") and seen["url"].split(":")[-1].split("/")[0] == seen["env"]["PORT"]


def test_the_node_gate_reports_a_wrong_answer_and_type_errors(monkeypatch, tmp_path):
    fake_serve(monkeypatch, "<html>")
    assert "/api/health did not answer" in gates.serve_node(tmp_path)
    fake_serve(monkeypatch, '{"status": "ok"}', tsc_code=2)
    assert "`npx tsc --noEmit` failed" in gates.serve_node(tmp_path)
