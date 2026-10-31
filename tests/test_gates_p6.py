"""The hygiene gates: secrets_clean, lint_clean and packages_exist (no network, no LLM)."""

import json
import sys
from pathlib import Path

import pytest

from factory.gates import GATES, Failure, GateCtx, declared_packages, find_secrets, mask
from factory.runbook import catalog
from factory.scaffold import Template

# Secrets are assembled at run time so that this file itself holds none.
AWS = "AKIA" + "Q3EGRZ7XBN4LMP2W"
GITHUB = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
SLACK = "xoxb-" + "1234567890-abcdefghij"
ANTHROPIC = "sk-ant-" + "api03-Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2"
OPENAI = "sk-proj-" + "Ab12Cd34Ef56Gh78Ij90Kl12Mn34Op56"
JWT = "eyJhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxMjM0NTY3ODkwIn0" + ".SflKxwRJSMeKKF2QT4fwpMeJf36P"
GENERIC = "q8Wm2Zr7Lp4Vn9Xc3Bt6"
BEARER = "Zm9vYmFyMTIzNDU2Nzg5MGFiY2RlZg"
PEM = "-----BEGIN RSA " + "PRIVATE KEY-----"


def template(tmp_path, commands=None, name="t"):
    path = tmp_path / "templates" / name
    path.mkdir(parents=True, exist_ok=True)
    return Template(name=name, path=path, surface="frontend", stack="", suite="vitest", serve=None,
                    commands=commands or {}, test_files=("src/*.test.js",), source_files=("src/*",),
                    protected=())


def ctx(tmp_path, commands=None, registry=None, dirs=("web",)):
    app = tmp_path / "app"
    for d in dirs:
        (app / d).mkdir(parents=True, exist_ok=True)
    tpls = {d: template(tmp_path, commands, d) for d in dirs}

    def no_network(url):
        raise AssertionError(f"the test tried to reach {url}")

    return GateCtx(app_dir=app, cwd=app / dirs[0], dirs=list(dirs), templates=tpls, artifacts={},
                   fetch=lambda url: None, registry=registry or no_network)


def write(root: Path, files: dict[str, str | bytes]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text) if isinstance(text, bytes) else p.write_text(text)


# --- secrets_clean ----------------------------------------------------------


def test_the_new_gates_are_registered_and_wired_into_web_app():
    assert {"secrets_clean", "lint_clean", "packages_exist"} <= set(GATES)
    steps = {s.id: s for n in catalog()["web-app"].steps for s in getattr(n, "steps", [n])}
    assert "secrets_clean" in steps["tests"].gates
    assert {"secrets_clean", "packages_exist", "lint_clean"} <= set(steps["develop"].gates)


def test_clean_code_passes(tmp_path):
    c = ctx(tmp_path)
    write(c.cwd, {
        "src/api.js": "const key = process.env.API_KEY\nconst token = import.meta.env.VITE_TOKEN\n",
        "src/config.py": 'API_KEY = os.environ["API_KEY"]\npassword = "short"\n',
        "src/App.test.js": "const token = 'test-token-value-1234567890'\n",  # a placeholder
        "README.md": "Set `api_key = \"your-api-key-goes-here-123\"` in .env\n",
    })
    GATES["secrets_clean"](c, {})


@pytest.mark.parametrize("line, label", [
    (f"aws_id = '{AWS}'", "AWS access key id"),
    (f"const t = '{GITHUB}'", "GitHub token"),
    (f"SLACK = '{SLACK}'", "Slack token"),
    (f"key: '{ANTHROPIC}'", "Anthropic API key"),
    (f"client = OpenAI(api_key='{OPENAI}')", "OpenAI API key"),
    (f"const jwt = '{JWT}'", "JWT"),
    (f"headers = {{'Authorization': 'Bearer {BEARER}'}}", "bearer token"),
    (f'const apiKey = "{GENERIC}"', "hard-coded credential"),
    (f'"client_secret": "{GENERIC}",', "hard-coded credential"),
    (PEM, "private key block"),
])
def test_each_kind_of_secret_is_found_and_masked(tmp_path, line, label):
    c = ctx(tmp_path)
    write(c.cwd, {"src/one.js": "// nothing here\n" + line + "\n"})
    with pytest.raises(Failure) as e:
        GATES["secrets_clean"](c, {})
    assert e.value.level == "step"
    assert f"src/one.js:2: {label}" in e.value.message
    assert "environment variable" in e.value.message  # feedback the agent can act on
    for secret in (AWS, GITHUB, SLACK, ANTHROPIC, OPENAI, JWT, GENERIC, BEARER):
        assert secret not in e.value.message
        assert secret[:8] not in e.value.message


def test_mask_shows_no_character_of_the_secret():
    assert mask(GENERIC) == "******** (20 chars)"
    assert mask("short") == "******** (5 chars)"


def test_env_files_are_checked_without_quotes(tmp_path):
    c = ctx(tmp_path)
    write(c.cwd, {".env": f"DEBUG=1\nSTRIPE_SECRET={GENERIC}\n"})
    assert find_secrets(c.cwd) == [".env:2: credential in an env file (******** (20 chars))"]


def test_ignored_folders_lock_files_and_binaries_are_skipped(tmp_path):
    c = ctx(tmp_path)
    secret = f"aws = '{AWS}'\n"
    write(c.cwd, {
        "node_modules/pkg/index.js": secret, ".venv/lib/x.py": secret, ".git/config": secret,
        ".factory/run.json": secret, "dist/bundle.js": secret, "package-lock.json": secret,
        "src/logo.png": b"\x89PNG\0\0" + secret.encode(),
    })
    assert find_secrets(c.cwd) == []
    write(c.cwd, {"src/leak.js": "\n\n" + secret})
    assert find_secrets(c.cwd) == [f"src/leak.js:3: AWS access key id ({mask(AWS)})"]


# --- lint_clean -------------------------------------------------------------


def lint_cmd(code: int) -> str:
    return f"{sys.executable} -c \"import sys; print('src/a.js:1 no-unused-vars'); sys.exit({code})\""


def test_no_lint_command_passes_silently(tmp_path):
    out = {}
    GATES["lint_clean"](ctx(tmp_path, {"test": "false"}), out)
    assert out == {}


def test_lint_passes_when_the_command_exits_0(tmp_path):
    GATES["lint_clean"](ctx(tmp_path, {"lint": lint_cmd(0)}), {})


def test_lint_failure_gives_the_tool_output_as_feedback(tmp_path):
    with pytest.raises(Failure) as e:
        GATES["lint_clean"](ctx(tmp_path, {"lint": lint_cmd(1)}), {"lint": "clean"})  # agent claims clean
    assert e.value.level == "step"
    assert "web:" in e.value.message and "exit code 1" in e.value.message
    assert "no-unused-vars" in e.value.message


def test_a_missing_lint_tool_stops_the_run(tmp_path):
    with pytest.raises(Failure) as e:
        GATES["lint_clean"](ctx(tmp_path, {"lint": "no-such-linter-xyz ."}), {})
    assert e.value.level == "run" and "cannot run" in e.value.message


def test_lint_runs_in_each_surface_of_the_step(tmp_path):
    c = ctx(tmp_path, {"lint": f"{sys.executable} -c \"import pathlib; pathlib.Path('linted').touch()\""},
            dirs=("web", "api"))
    GATES["lint_clean"](c, {})
    assert (c.app_dir / "web/linted").exists() and (c.app_dir / "api/linted").exists()


# --- packages_exist ---------------------------------------------------------

REAL = {"react", "vitest", "@testing-library/react", "fastapi", "pydantic-settings", "pytest"}


def registry(answers=None):
    """A fake registry: known names answer 200, others 404; `answers` overrides by name."""
    asked = []

    def probe(url):
        asked.append(url)
        name = url.removeprefix("https://registry.npmjs.org/").removeprefix("https://pypi.org/pypi/")
        name = name.removesuffix("/json").replace("%2F", "/")
        if answers and name in answers:
            return answers[name]
        return 200 if name in REAL else 404

    probe.asked = asked
    return probe


def manifests(root, npm=None, dev=None, pyproject=None, requirements=None):
    if npm is not None or dev is not None:
        write(root, {"package.json": json.dumps({"dependencies": npm or {}, "devDependencies": dev or {}})})
    if pyproject is not None:
        write(root, {"pyproject.toml": pyproject})
    if requirements is not None:
        write(root, {"requirements.txt": requirements})


def test_real_packages_pass(tmp_path):
    probe = registry()
    c = ctx(tmp_path, registry=probe)
    manifests(c.cwd, npm={"react": "^19"}, dev={"vitest": "^5", "@testing-library/react": "^16"},
              pyproject='[project]\ndependencies = ["fastapi>=0.115", "pydantic_settings[dotenv]"]\n'
                        '[dependency-groups]\ndev = ["pytest>=8", {include-group = "x"}]\n')
    out = {}
    GATES["packages_exist"](c, out)
    assert out["unverified_packages"] == []
    assert "https://registry.npmjs.org/@testing-library%2Freact" in probe.asked
    assert "https://pypi.org/pypi/pydantic-settings/json" in probe.asked  # normalized name


def test_invented_packages_fail_with_their_names(tmp_path):
    c = ctx(tmp_path, registry=registry())
    manifests(c.cwd, npm={"react": "^19", "react-magic-forms-pro": "^1"},
              requirements="fastapi\nfastapi-auto-crud>=2  # invented\n-r other.txt\n./local\n")
    with pytest.raises(Failure) as e:
        GATES["packages_exist"](c, {})
    assert e.value.level == "step"
    assert "npm:react-magic-forms-pro" in e.value.message and "pypi:fastapi-auto-crud" in e.value.message
    assert "npm:react," not in e.value.message and "pypi:fastapi," not in e.value.message


def test_a_network_error_is_unverified_not_missing(tmp_path, caplog):
    c = ctx(tmp_path, registry=registry({"react": None, "fastapi": 503}))
    manifests(c.cwd, npm={"react": "^19"}, pyproject='[project]\ndependencies = ["fastapi"]\n')
    out = {}
    GATES["packages_exist"](c, out)  # passes
    assert out["unverified_packages"] == ["npm:react", "pypi:fastapi"]
    assert "could not verify" in caplog.text


def test_an_unknown_package_still_fails_when_others_are_unverified(tmp_path):
    c = ctx(tmp_path, registry=registry({"react": None}))
    manifests(c.cwd, npm={"react": "^19", "nope-not-real": "1"})
    out = {}
    with pytest.raises(Failure, match="npm:nope-not-real"):
        GATES["packages_exist"](c, out)
    assert out["unverified_packages"] == ["npm:react"]


def test_no_manifest_asks_no_registry(tmp_path):
    GATES["packages_exist"](ctx(tmp_path), {})  # the default registry fake fails on any call


def test_non_registry_specs_are_not_checked(tmp_path):
    root = tmp_path / "p"
    manifests(root, npm={"local": "file:../local", "fork": "github:me/fork", "short": "me/fork",
                         "web": "https://x.org/a.tgz", "alias": "npm:react@^19", "ws": "workspace:*"},
              pyproject='[project]\ndependencies = ["pkg @ https://x.org/pkg.whl", "Flask>=3"]\n'
                        '[tool.poetry.dependencies]\npython = "^3.11"\nlocal = {path = "../l"}\nrich = "^13"\n')
    assert declared_packages(root) == ({"react"}, {"flask", "rich"})


def test_a_broken_manifest_is_step_feedback(tmp_path):
    c = ctx(tmp_path, registry=registry())
    write(c.cwd, {"package.json": "{not json"})
    with pytest.raises(Failure, match="manifest cannot be read") as e:
        GATES["packages_exist"](c, {})
    assert e.value.level == "step"


def test_the_shipped_templates_declare_their_packages():
    from factory.paths import TEMPLATES_DIR

    npm, _ = declared_packages(TEMPLATES_DIR / "react-vite")
    _, pypi = declared_packages(TEMPLATES_DIR / "fastapi")
    assert {"react", "vite", "vitest"} <= npm
    assert pypi == {"fastapi", "uvicorn", "pytest", "httpx"}


# --- the registry probe, with urlopen replaced (no network) ------------------


@pytest.mark.parametrize("behaviour, want", [
    ("200", 200),
    ("404", 404),
    ("503", 503),
    ("timeout", None),
    ("dns", None),
    ("badstatusline", None),
])
def test_probe_maps_answers_and_network_errors(monkeypatch, behaviour, want):
    import http.client
    import urllib.error
    import urllib.request

    from factory import gates

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        assert timeout == gates.REGISTRY_TIMEOUT
        if behaviour == "200":
            return Resp()
        if behaviour in ("404", "503"):
            raise urllib.error.HTTPError(req.full_url, int(behaviour), "x", {}, None)
        if behaviour == "timeout":
            raise TimeoutError("timed out")
        if behaviour == "dns":
            raise urllib.error.URLError("no such host")
        raise http.client.BadStatusLine("garbage")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert gates.probe_registry("https://registry.npmjs.org/react") == want


def test_a_timeout_during_the_gate_passes_with_a_note(tmp_path, monkeypatch, caplog):
    import urllib.request

    from factory import gates

    def offline(req, timeout):
        raise TimeoutError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    c = ctx(tmp_path, registry=gates.probe_registry)
    manifests(c.cwd, npm={"react": "^19"})
    out = {}
    GATES["packages_exist"](c, out)
    assert out["unverified_packages"] == ["npm:react"]
    assert "npm:react" in caplog.text
