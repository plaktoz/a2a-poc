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
import json
import re
import socket
import subprocess
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .process import run, tail
from .scaffold import Template


class Failure(Exception):
    def __init__(self, message: str, level: str = "step") -> None:
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


@dataclass
class GateCtx:
    app_dir: Path  # the run's workspace
    cwd: Path  # where the agent worked
    dirs: list[str]  # the surfaces (scaffold directories) this step covers
    templates: dict[str, Template]  # scaffold directory -> its template
    artifacts: dict[str, Any]
    item: dict | None
    fetch: Callable[[str], str | None]


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


def _lines(items: list[str], limit: int = 4000) -> str:
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
        raise Failure(_lines(problems))


@gate
def tests_red(ctx: GateCtx, out: dict) -> None:
    """New tests load and fail: they drive code that does not exist yet."""
    r = run_tests(ctx)
    if r.load_errors:
        raise Failure("the tests do not load:\n" + _lines(r.load_errors))
    if r.total == 0:
        raise Failure("no tests were collected")
    if r.passed == r.total:
        raise Failure("every test passes before anything is built, so no test drives new code")


@gate
def tests_green(ctx: GateCtx, out: dict) -> None:
    r = run_tests(ctx)
    if r.load_errors or r.total == 0:
        raise Failure("the tests do not load:\n" + _lines(r.load_errors), "round")
    if r.failed:
        raise Failure(f"{r.failed} test(s) still fail:\n" + _lines(r.failures), "round")


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
        why = _lines([f"- {r['claim']} [{r['source_url']}]: {r['reason']}" for r in rejected]) or "none given"
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
        raise Failure("cite each verified source where its claim is, and nothing else:\n" + _lines(problems))


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
                      "the line it is on:\n" + _lines([f"- {r['title']}: {r['reason']}" for r in rejected]))


@gate
def review_approves(ctx: GateCtx, out: dict) -> None:
    if out["verdict"] != "approve":
        issues = "\n".join(f"- {i}" for i in out.get("issues", [])) or "(no issues listed)"
        raise Failure("the reviewer asked for changes:\n" + issues, "round")
