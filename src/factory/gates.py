"""Gates: checks the orchestrator runs on an agent's output before the run moves on.

A gate passes by returning and fails by raising Failure. `level` says what happens next:

- "step":  the same agent is asked again, with the message as feedback (up to the step's retries)
- "round": the enclosing loop starts another round, with the message as feedback
- "run":   the run stops, because more of the same work will not fix it (for example a test gap)

The orchestrator runs every test suite itself; a gate never trusts a count an agent reports.
Gates that check evidence (citations, audit findings) move what they cannot verify to the
output's `rejected` list instead of failing, and fail only when nothing usable is left.
"""

from __future__ import annotations

import html
import http.client
import json
import logging
import os
import re
import shlex
import socket
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote as url_quote

from .process import run, tail
from .scaffold import IGNORED, Template

log = logging.getLogger(__name__)


Level = Literal["step", "round", "run"]


class Failure(Exception):
    def __init__(self, message: str, level: Level = "step") -> None:
        super().__init__(message)
        self.message, self.level = message, level


@dataclass
class TestRun:
    total: int = 0
    passed: int = 0
    failures: list[str] = field(default_factory=list)
    load_errors: list[str] = field(default_factory=list)

    @property
    def failed(self) -> int:
        return self.total - self.passed


REGISTRY_TIMEOUT = 10
NPM_REGISTRY = "https://registry.npmjs.org/"
PYPI_REGISTRY = "https://pypi.org/pypi/"


def probe_registry(url: str) -> int | None:
    """The HTTP status a package registry answers for `url`, or None if it cannot be reached.

    Only the status is read. The body (a package's full metadata) is not downloaded.
    """
    # The abbreviated npm document is small; the registry answers 404 the same way for both.
    headers = {"User-Agent": "factory-gates/0.1", "Accept": "application/vnd.npm.install-v1+json, application/json"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=REGISTRY_TIMEOUT) as resp:
            return resp.status
    except urllib.error.HTTPError as e:  # an HTTP answer, such as 404 for an unknown package
        return e.code
    except (OSError, ValueError, http.client.HTTPException):
        return None


@dataclass
class GateCtx:
    app_dir: Path  # the run's workspace
    cwd: Path  # where the agent worked
    dirs: list[str]  # the surfaces (scaffold directories) this step covers
    templates: dict[str, Template]  # scaffold directory -> its template
    artifacts: dict[str, Any]
    fetch: Callable[[str], str | None]
    registry: Callable[[str], int | None] = probe_registry  # HTTP status of a registry URL; tests inject
    tree: Path | None = None  # the repo worktree of a change request (P10)
    test_command: str | None = None  # the repo's own test command, from --test-command (P10)


GATES: dict[str, Callable[[GateCtx, dict], None]] = {}


def gate(fn: Callable[[GateCtx, dict], None]) -> Callable[[GateCtx, dict], None]:
    GATES[fn.__name__] = fn
    return fn


# --- test suites ------------------------------------------------------------


def vitest(wd: Path) -> TestRun:
    report = wd / ".factory" / "vitest.json"
    report.parent.mkdir(exist_ok=True)
    report.unlink(missing_ok=True)
    r = run(["npx", "vitest", "run", "--reporter=json", f"--outputFile={report}"], cwd=wd)
    if not report.exists():
        raise RuntimeError(f"vitest wrote no report in {wd.name}:\n{tail(r)}")
    data = json.loads(report.read_text())
    out = TestRun(data["numTotalTests"], data["numPassedTests"])
    for suite in data["testResults"]:
        if suite.get("message") and not suite["assertionResults"]:
            out.load_errors.append(f"{Path(suite['name']).name} failed to load: {suite['message'][:1500]}")
        for a in suite["assertionResults"]:
            if a["status"] != "passed":
                msg = " ".join(a.get("failureMessages") or [a["status"]])
                out.failures.append(f"{a['fullName']}: {msg[:800]}")
    return out


def pytest(wd: Path) -> TestRun:
    report = wd / ".factory" / "junit.xml"
    report.parent.mkdir(exist_ok=True)
    report.unlink(missing_ok=True)
    r = run([".venv/bin/pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={report}"], cwd=wd)
    if not report.exists():
        raise RuntimeError(f"pytest wrote no report in {wd.name}:\n{tail(r)}")
    out = TestRun()
    for case in ET.parse(report).getroot().iter("testcase"):
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        bad = case.find("failure")
        if bad is None:
            bad = case.find("error")
        message = "" if bad is None else (bad.get("message") or bad.text or "")
        if message.startswith("collection failure"):
            out.load_errors.append(f"{name} failed to load: {(bad.text or message)[:1500]}")
            continue
        out.total += 1
        if bad is not None:
            out.failures.append(f"{name}: {message[:800]}")
        elif case.find("skipped") is not None:
            out.failures.append(f"{name}: skipped")
        else:
            out.passed += 1
    return out


SUITES: dict[str, Callable[[Path], TestRun]] = {"vitest": vitest, "pytest": pytest}


def run_tests(ctx: GateCtx) -> TestRun:
    """Run the suite of every surface this step covers, and add the results up."""
    total = TestRun()
    for d in ctx.dirs:
        r = SUITES[ctx.templates[d].suite](ctx.app_dir / d)
        total.total += r.total
        total.passed += r.passed
        total.failures += [f"[{d}] {x}" for x in r.failures]
        total.load_errors += [f"[{d}] {x}" for x in r.load_errors]
    return total


def _capped(items: list[str], limit: int = 4000) -> str:
    return "\n".join(items)[:limit]


# --- servers ----------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def _serve(argv: list[str], cwd: Path, url: str, check: Callable[[str], str | None]) -> str | None:
    """Start a server, wait for `url`, and return check(body), or what went wrong."""
    proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                return check(_get(url)[1])
            except OSError:
                if proc.poll() is not None:
                    return f"`{' '.join(argv[:3])}` exited with code {proc.returncode}"
                time.sleep(0.5)
        return f"nothing served {url} within 30s"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def serve_vite(wd: Path) -> str | None:
    """None if the Vite app builds and its production bundle is served; else what went wrong."""
    r = run(["npm", "run", "build"], cwd=wd)
    if r.returncode != 0:
        return f"`npm run build` failed:\n{tail(r)}"
    base = f"http://127.0.0.1:{free_port()}"

    def check(page: str) -> str | None:
        scripts = re.findall(r'src="(/assets/[^"]+\.js)"', page)
        if 'id="root"' not in page or not scripts:
            return f"the served index.html has no root element or JS bundle:\n{page[:500]}"
        for s in scripts:
            if (status := _get(base + s)[0]) != 200:
                return f"bundle {s} returned HTTP {status}"
        return None

    # Run vite directly, not through npx, so terminate() reaches the server process.
    argv = [str(wd / "node_modules/.bin/vite"), "preview", "--host", "127.0.0.1",
            "--port", base.rsplit(":", 1)[1], "--strictPort"]
    return _serve(argv, wd, base + "/", check)


def serve_asgi(wd: Path) -> str | None:
    """None if the ASGI app starts under uvicorn and serves its OpenAPI schema."""
    port = free_port()

    def check(body: str) -> str | None:
        try:
            paths = json.loads(body).get("paths", {})
        except ValueError:
            return "/openapi.json is not JSON"
        return None if paths else "the API serves no routes"

    argv = [".venv/bin/uvicorn", "app.main:create_app", "--factory",
            "--host", "127.0.0.1", "--port", str(port)]
    return _serve(argv, wd, f"http://127.0.0.1:{port}/openapi.json", check)


SERVERS: dict[str, Callable[[Path], str | None]] = {"vite": serve_vite, "asgi": serve_asgi}


# --- development gates ------------------------------------------------------


_SKIPS = {
    "vitest": re.compile(r"\b(?:it|test|describe)\s*\.\s*(?:skip|only|todo)\b|\bx(?:it|test|describe)\s*\("),
    "pytest": re.compile(r"pytest\.mark\.(?:skip|skipif|xfail)\b|pytest\.(?:skip|xfail)\s*\("),
}


def test_names(suite: str, cid: str) -> re.Pattern[str]:
    """How a test is named for criterion `cid`: it('AC3: ...') or def test_ac3_...()."""
    n = cid[2:]
    if suite == "vitest":
        return re.compile(rf"\b(?:it|test)\s*\(\s*(['\"`])AC{n}\s*:")
    return re.compile(rf"^\s*(?:async\s+)?def\s+test_ac{n}_\w*\s*\(", re.M)


def _criteria(ctx: GateCtx, surface: str) -> list[str]:
    return [c["id"] for c in ctx.artifacts["spec"]["acceptance_criteria"] if c.get("surface") == surface]


@gate
def tests_named(ctx: GateCtx, out: dict) -> None:
    """Every criterion of each surface has a test named for it, and nothing is skipped."""
    problems = []
    for d in ctx.dirs:
        tpl, wd = ctx.templates[d], ctx.app_dir / d
        text = "\n".join(p.read_text() for g in tpl.test_files for p in sorted(wd.glob(g)) if p.is_file())
        if not text:
            problems.append(f"{d}: no test file matching {', '.join(tpl.test_files)}")
            continue
        if _SKIPS[tpl.suite].search(text):
            problems.append(f"{d}: tests must not be skipped, focused or marked todo")
        if missing := [i for i in _criteria(ctx, d) if not test_names(tpl.suite, i).search(text)]:
            example = "it('AC1: ...')" if tpl.suite == "vitest" else "def test_ac1_...()"
            problems.append(f"{d}: no test named for {', '.join(missing)} (name them like {example})")
    if problems:
        raise Failure(_capped(problems))


@gate
def tests_red(ctx: GateCtx, out: dict) -> None:
    """New tests load and fail: they drive code that does not exist yet."""
    r = run_tests(ctx)
    if r.load_errors:
        raise Failure("the tests do not load:\n" + _capped(r.load_errors))
    if r.total == 0:
        raise Failure("no tests were collected")
    if r.passed == r.total:
        raise Failure("every test passes before anything is built, so no test drives new code")


@gate
def tests_green(ctx: GateCtx, out: dict) -> None:
    r = run_tests(ctx)
    if r.load_errors or r.total == 0:
        raise Failure("the tests do not load:\n" + _capped(r.load_errors), "round")
    if r.failed:
        raise Failure(f"{r.failed} test(s) still fail:\n" + _capped(r.failures), "round")


@gate
def counts_match(ctx: GateCtx, out: dict) -> None:
    """The verifier's counts must be the ones the orchestrator's own run produced."""
    r = run_tests(ctx)
    if (out["passed"], out["failed"]) != (r.passed, r.failed):
        raise Failure(f"you reported passed={out['passed']} failed={out['failed']}, but the test run "
                      f"says passed={r.passed} failed={r.failed}. Copy the counts from the run.")


@gate
def serves(ctx: GateCtx, out: dict) -> None:
    """Every surface builds and its server starts and serves."""
    for d in ctx.dirs:
        server = ctx.templates[d].serve
        if server and (problem := SERVERS[server](ctx.app_dir / d)):
            raise Failure(f"{d}: {problem}", "round")


@gate
def criteria_covered(ctx: GateCtx, out: dict) -> None:
    """Every criterion of the step's surfaces passes. With green tests, a gap is a test gap."""
    verdicts = out.get("verdicts", {})
    ids = [i for d in ctx.dirs for i in _criteria(ctx, d)]
    if gaps := [i for i in ids if not str(verdicts.get(i, "")).upper().startswith("PASS")]:
        raise Failure(
            "the verifier did not accept these criteria as covered by a passing test: "
            + ", ".join(gaps) + ". The tests are green, so this is a test gap, not a code bug.",
            "run",
        )


# --- hygiene gates ----------------------------------------------------------
#
# Cheap checks the orchestrator runs on the files an agent wrote: secrets, lint, and package
# names. Like the test gates, they read the files themselves and trust nothing the agent reports.


def mask(secret: str) -> str:
    """A secret as it may be shown: its length only. No character of the value is revealed."""
    return f"{'*' * 8} ({len(secret)} chars)"


# (label, pattern, fuzzy). The secret is group 1 if the pattern has one, else the whole match.
# Fuzzy patterns match any long value after a name such as `token =`, so they skip placeholders.
SECRET_PATTERNS: list[tuple[str, re.Pattern[str], bool]] = [
    ("private key block", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"), False),
    ("AWS access key id", re.compile(r"\b((?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA)[0-9A-Z]{16})\b"), False),
    ("GitHub token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"), False),
    ("Slack token", re.compile(r"\b(xox[abprs]-[A-Za-z0-9-]{10,})"), False),
    ("Anthropic API key", re.compile(r"\b(sk-ant-[A-Za-z0-9_-]{20,})"), False),
    ("OpenAI API key", re.compile(r"\b(sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,})"), False),
    ("JWT", re.compile(r"\b(eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"), False),
    ("bearer token", re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{20,})"), True),
    ("hard-coded credential", re.compile(
        r"""(?i)(?:api[_-]?key|access[_-]?key|secret|token|passwd|password)\w*["']?\s*[:=]\s*["']([^"'\s]{16,})["']"""), True),
]
# In a .env file values are not quoted.
ENV_LINE = re.compile(r"(?i)^\s*(?:export\s+)?\w*(?:api_?key|secret|token|passw(?:or)?d)\w*\s*=\s*([^\s'\"#()]{16,})")
PLACEHOLDER = re.compile(r"(?i)example|placeholder|changeme|dummy|fake|sample|redacted|your[-_ ]|xxxx|test|\*\*\*")
SCAN_SKIP_FILES = {"package-lock.json", "uv.lock", "yarn.lock", "pnpm-lock.yaml", "poetry.lock"}
SCAN_MAX_BYTES = 1_000_000


def _real_looking(value: str) -> bool:
    """A value that could be a secret: letters and digits, not a URL, a variable or a placeholder."""
    return (any(c.isalpha() for c in value) and any(c.isdigit() for c in value) and "://" not in value
            and not value.startswith(("$", "{", "<", "%")) and not PLACEHOLDER.search(value))


def find_secrets(root: Path) -> list[str]:
    """One line per suspected secret under `root`: `path:line: what (masked value)`.

    Binary files, big files, lock files and the folders in scaffold.IGNORED (node_modules,
    .venv, .git, .factory, ...) are skipped. The folders are pruned, so they are never walked.
    """
    found: list[str] = []
    if not root.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED)
        for name in sorted(filenames):
            path = Path(dirpath, name)
            if name in SCAN_SKIP_FILES or path.is_symlink() or path.stat().st_size > SCAN_MAX_BYTES:
                continue
            raw = path.read_bytes()
            if b"\0" in raw[:8000]:
                continue
            found += _scan_text(path.relative_to(root).as_posix(), raw.decode("utf-8", "replace"))
    return found


def _scan_text(rel: str, text: str) -> list[str]:
    found = []
    in_env = rel.rsplit("/", 1)[-1].startswith(".env")
    for n, line in enumerate(text.splitlines(), 1):
        for label, pattern, fuzzy in SECRET_PATTERNS:
            m = pattern.search(line)
            if m:
                value = m.group(m.lastindex or 0)
                if fuzzy and not _real_looking(value):
                    continue
                shown = "" if label == "private key block" else f" ({mask(value)})"
                found.append(f"{rel}:{n}: {label}{shown}")
                break
        else:
            if in_env and (m := ENV_LINE.match(line)) and _real_looking(m.group(1)):
                found.append(f"{rel}:{n}: credential in an env file ({mask(m.group(1))})")
    return found


@gate
def secrets_clean(ctx: GateCtx, out: dict) -> None:
    """No file the agent wrote holds what looks like a secret. The message masks every value."""
    if found := find_secrets(ctx.cwd):
        raise Failure("these lines look like secrets. Remove them: read each value from an "
                      "environment variable, and never write a real key, token or password into a "
                      "file:\n" + _capped([f"- {f}" for f in found]))


@gate
def lint_clean(ctx: GateCtx, out: dict) -> None:
    """The template's `lint` command exits 0 in every surface this step covers.

    The orchestrator runs it, so the agent cannot report a pass. A template without a `lint`
    command has nothing to check, and passes.
    """
    for d in ctx.dirs:
        command = ctx.templates[d].commands.get("lint")
        if not command:
            continue
        try:
            r = run(shlex.split(command), cwd=ctx.app_dir / d)
        except FileNotFoundError as e:  # the tool is not installed: not the agent's doing
            raise Failure(f"{d}: lint command `{command}` cannot run: {e}", "run") from None
        except subprocess.TimeoutExpired:
            raise Failure(f"{d}: `{command}` did not finish within the time limit", "step") from None
        if r.returncode != 0:
            raise Failure(f"{d}: `{command}` found problems (exit code {r.returncode}). Fix them:\n"
                          + tail(r, 3000))


@gate
def repo_tests_pass(ctx: GateCtx, out: dict) -> None:
    """The repo's own test command (--test-command) exits 0 in the worktree.

    The orchestrator runs it, so an agent cannot report a pass. The command is split into
    arguments and run without a shell, as the template commands are.
    """
    if ctx.tree is None or not ctx.test_command:
        raise Failure("no test command: pass --test-command with the command that runs the repo's tests", "run")
    try:
        r = run(shlex.split(ctx.test_command), cwd=ctx.tree)
    except FileNotFoundError as e:  # the tool is not installed: not the agent's doing
        raise Failure(f"the test command `{ctx.test_command}` cannot run: {e}", "run") from None
    except subprocess.TimeoutExpired:
        raise Failure(f"`{ctx.test_command}` did not finish within the time limit", "round") from None
    if r.returncode != 0:
        raise Failure(f"`{ctx.test_command}` failed (exit code {r.returncode}). Fix the code so it passes:\n"
                      + tail(r, 3000), "round")


@gate
def repro_fails(ctx: GateCtx, out: dict) -> None:
    """The repo's test command fails, and its output names the reproduction test (bug-fix).

    The orchestrator runs it, so an agent cannot claim a failure. A suite that passes means the bug
    is not reproduced. A suite that fails without naming the test fails for another reason.
    """
    if ctx.tree is None or not ctx.test_command:
        raise Failure("no test command: pass --test-command with the command that runs the repo's tests", "run")
    try:
        r = run(shlex.split(ctx.test_command), cwd=ctx.tree)
    except FileNotFoundError as e:  # the tool is not installed: not the agent's doing
        raise Failure(f"the test command `{ctx.test_command}` cannot run: {e}", "run") from None
    except subprocess.TimeoutExpired:
        raise Failure(f"`{ctx.test_command}` did not finish within the time limit", "step") from None
    if r.returncode == 0:
        raise Failure("the bug is not reproduced: every test passes. Write a test that fails because of the "
                      "bug, and do not change the source code.")
    name = out.get("test_name", "")
    if name not in (r.stdout or "") + (r.stderr or ""):
        raise Failure(f"the failing output does not name your test {name!r}; the suite fails for another reason. "
                      "Write one test that fails because of the bug, and report its exact name.\n" + tail(r, 3000))


_NPM_NON_REGISTRY = re.compile(r"^(?:(?:file|link|workspace|portal|patch|git|git\+\w+|https?|github|gitlab|bitbucket):"
                               r"|[\w.-]+/[\w.-]+(?:#.*)?$)")
_REQ_NAME = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)")


def _npm_names(manifest: Path) -> set[str]:
    data = json.loads(manifest.read_text())
    names = set()
    for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        for name, spec in (data.get(section) or {}).items():
            spec = str(spec)
            if spec.startswith("npm:"):  # an alias: the real package is after `npm:`
                name = spec[4:].rpartition("@")[0] or spec[4:]
            elif _NPM_NON_REGISTRY.match(spec):
                continue
            names.add(name)
    return names


def _requirement_name(spec: str) -> str | None:
    """The package named by a PEP 508 requirement, or None for a path, URL or option."""
    spec = spec.split("#", 1)[0].strip()
    if not spec or spec.startswith(("-", ".", "/", "~")) or "://" in spec or re.search(r"\s@\s", spec):
        return None
    m = _REQ_NAME.match(spec)
    return re.sub(r"[-_.]+", "-", m.group(1)).lower() if m else None


def _pypi_names(root: Path) -> set[str]:
    specs: list[str] = []
    files = [*root.glob("requirements*.txt"), *root.glob("requirements/*.txt")]
    for f in files:
        specs += f.read_text(errors="replace").splitlines()
    if (pyproject := root / "pyproject.toml").is_file():
        data = tomllib.loads(pyproject.read_text())
        project = data.get("project", {})
        specs += project.get("dependencies", [])
        for group in (project.get("optional-dependencies") or {}).values():
            specs += group
        for group in (data.get("dependency-groups") or {}).values():
            specs += [x for x in group if isinstance(x, str)]  # the others are {include-group: ...}
        specs += data.get("tool", {}).get("uv", {}).get("dev-dependencies", [])
        poetry = data.get("tool", {}).get("poetry", {})
        for table in (poetry.get("dependencies"), poetry.get("dev-dependencies"),
                      *(g.get("dependencies") for g in (poetry.get("group") or {}).values())):
            specs += [k for k, v in (table or {}).items()
                      if k.lower() != "python" and not (isinstance(v, dict) and ("path" in v or "git" in v or "url" in v))]
    return {n for s in specs if (n := _requirement_name(s))}


def declared_packages(root: Path) -> tuple[set[str], set[str]]:
    """The (npm, PyPI) package names that the manifests in `root` ask for."""
    npm = _npm_names(root / "package.json") if (root / "package.json").is_file() else set()
    return npm, _pypi_names(root)


@gate
def packages_exist(ctx: GateCtx, out: dict) -> None:
    """Every npm and PyPI package the manifests in each surface name exists on its registry.

    Guards against invented package names. A package is unknown only if its registry answers 404.
    If the registry cannot be reached (no network, a timeout, or any other status such as 5xx or
    429) the package is unverified: the gate still passes, logs a warning, and lists the names in
    out["unverified_packages"]. An outage must not look like an invented package.
    """
    wanted: dict[str, str] = {}  # registry URL -> "npm:name" or "pypi:name"
    for d in ctx.dirs:
        try:
            npm, pypi = declared_packages(ctx.app_dir / d)
        except (ValueError, OSError) as e:  # bad JSON or TOML in a manifest
            raise Failure(f"{d}: a package manifest cannot be read: {e}") from None
        for name in sorted(npm):
            wanted[NPM_REGISTRY + url_quote(name, safe="@")] = f"npm:{name}"
        for name in sorted(pypi):
            wanted[f"{PYPI_REGISTRY}{name}/json"] = f"pypi:{name}"
    if not wanted:
        return
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = dict(zip(wanted, pool.map(ctx.registry, wanted)))
    unknown = [wanted[u] for u, st in statuses.items() if st == 404]
    unverified = sorted(wanted[u] for u, st in statuses.items() if st != 200 and st != 404)
    out["unverified_packages"] = unverified
    if unverified:
        log.warning("packages_exist: could not verify %s", ", ".join(unverified))
    if unknown:
        raise Failure("these packages do not exist on their registry, so the names are invented: "
                      + ", ".join(sorted(unknown)) + ". Use a real package, or write the code without it.")


# --- evidence gates ---------------------------------------------------------


def strip_html(body: str) -> str:
    body = re.sub(r"(?is)<(script|style|sup)\b.*?</\1>", " ", body)  # sup: citation markers
    return html.unescape(re.sub(r"<[^>]+>", " ", body))


def fetch_text(url: str, timeout: int = 20) -> str | None:
    """The text of the page at `url`, without HTML tags, or None if it cannot be fetched."""
    if not url.startswith(("http://", "https://")):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "factory-research/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(5_000_000).decode("utf-8", "replace")
    except (OSError, ValueError):  # URLError and HTTPError are OSErrors
        return None
    head = body[:2000].lower()
    return strip_html(body) if "<html" in head or "<!doctype html" in head else body


def normalize(text: str) -> str:
    """Lowercase words only, so quotes survive differences in punctuation, quotes and spacing."""
    text = re.sub(r"\[(?:\d+|[a-z]|citation needed|note \d+)\]", " ", html.unescape(text).lower())
    return " ".join(re.findall(r"[^\W_]+", text))


def quote_on_page(quote: str, page: str) -> bool:
    q = normalize(quote)
    return len(q) >= 20 and q in normalize(page)


@gate
def citations_verified(ctx: GateCtx, out: dict) -> None:
    """Keep only findings whose quote appears on the page they cite."""
    kept, rejected = [], out.setdefault("rejected", [])
    for f in out.get("findings", []):
        page = ctx.fetch(f["source_url"])
        if page is not None and quote_on_page(f["quote"], page):
            kept.append({**f, "verified": True})
        else:
            why = "the page could not be fetched" if page is None else "the quote is not on the page"
            rejected.append({**f, "verified": False, "reason": why})
    out["findings"] = kept
    if not kept:
        why = _capped([f"- {r['claim']} [{r['source_url']}]: {r['reason']}" for r in rejected]) or "none given"
        raise Failure("no finding could be verified against its source. Each quote must be a sentence "
                      "copied exactly from the page at source_url. Rejected:\n" + why)


_LINK = re.compile(r"https?://[^\s)\]>\"']+")


def verified_sources(artifacts: dict[str, Any]) -> set[str]:
    findings = artifacts.get("findings", {})
    return {f["source_url"] for fs in findings.values() for f in fs.get("findings", [])}


@gate
def sources_cited(ctx: GateCtx, out: dict) -> None:
    """The report links every verified source, and no source that was not verified."""
    sources, markdown = verified_sources(ctx.artifacts), out["markdown"]
    linked = {u.rstrip(".,;:") for u in _LINK.findall(markdown)}
    problems = [f"- not linked: {u}" for u in sorted(sources - linked)]
    problems += [f"- not a verified source: {u}" for u in sorted(linked - sources)]
    if problems:
        raise Failure("cite each verified source where its claim is, and nothing else:\n" + _capped(problems))


@gate
def evidence_verified(ctx: GateCtx, out: dict) -> None:
    """Keep only audit findings whose evidence is in the cited file, near the cited line."""
    root = ctx.cwd.resolve()
    kept, rejected = [], out.setdefault("rejected", [])
    for f in out.get("findings", []):
        path = (root / f["file"]).resolve()
        why = None
        if not path.is_relative_to(root) or not path.is_file():
            why = "no such file in the project"
        else:
            lines = path.read_text(errors="replace").splitlines()
            near = "\n".join(lines[max(0, f["line"] - 6): f["line"] + 5])
            if normalize(f["evidence"]) not in normalize(near):
                why = f"the evidence is not near line {f['line']}"
        if why:
            rejected.append({**f, "verified": False, "reason": why})
        else:
            kept.append({**f, "verified": True})
    out["findings"] = kept
    if rejected and not kept:
        raise Failure("no finding could be verified. Copy evidence exactly from the file, and give "
                      "the line it is on:\n" + _capped([f"- {r['title']}: {r['reason']}" for r in rejected]))


@gate
def link_verified(ctx: GateCtx, out: dict) -> None:
    """Keep only service links whose evidence is in the cited file of this repo, near the cited line.

    The same check as evidence_verified, for a link between services (impact-analysis). A repo that
    has no link at all is a valid result: the gate accepts it."""
    root = ctx.cwd.resolve()
    kept, rejected = [], out.setdefault("rejected", [])
    for link in out.get("links", []):
        path = (root / link["file"]).resolve()
        why = None
        if not path.is_relative_to(root) or not path.is_file():
            why = "no such file in this repo"
        else:
            lines = path.read_text(errors="replace").splitlines()
            near = "\n".join(lines[max(0, link["line"] - 6): link["line"] + 5])
            if normalize(link["evidence"]) not in normalize(near):
                why = f"the evidence is not near line {link['line']}"
        if why:
            rejected.append({**link, "verified": False, "reason": why})
        else:
            kept.append({**link, "verified": True})
    out["links"] = kept
    if rejected and not kept:
        raise Failure("no link could be verified. Copy the evidence exactly from the file, and give the "
                      "line it is on:\n" + _capped([f"- {r['kind']} {r['target']}: {r['reason']}"
                                                    for r in rejected]))


@gate
def review_approves(ctx: GateCtx, out: dict) -> None:
    if out["verdict"] != "approve":
        issues = "\n".join(f"- {i}" for i in out.get("issues", [])) or "(no issues listed)"
        raise Failure("the reviewer asked for changes:\n" + issues, "round")
