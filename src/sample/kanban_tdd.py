"""Build a kanban board test-first with three factory workers.

Per slice (one red -> green cycle through the Board public API):
  1. codex-gpt          writes ONE new test              (edits test_board.py only)
  2. codex-gemini       runs the tests -> must be RED    (one new failure, rest pass)
  3. claude-agent-sdk   writes the minimum code          (edits board.py only)
  4. codex-gemini       runs the tests -> must be GREEN

After every step the driver checks that the worker stayed in its role (hashes of
the other file) and cross-checks Gemini's reported result with its own pytest run.

Usage: uv run python src/sample/kanban_tdd.py
Writes to src/sample/kanban/, overwriting any previous run.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

from factory import ConfigError, claude_env, codex_argv, load_config

ROOT = Path(__file__).resolve().parents[2]
WORKDIR = Path(__file__).resolve().parent / "kanban"
BOARD, TESTS = WORKDIR / "board.py", WORKDIR / "test_board.py"

TIMEOUT = 300
TEST_WRITER, IMPLEMENTER, TEST_RUNNER = "codex-gpt", "claude-agent-sdk", "codex-gemini"

SKELETON = '''\
from __future__ import annotations

from dataclasses import dataclass


class UnknownCard(Exception):
    pass


class UnknownColumn(Exception):
    pass


class WipLimitExceeded(Exception):
    pass


@dataclass(frozen=True)
class Card:
    id: int
    title: str


class Board:
    def __init__(self, columns: list[str], wip_limits: dict[str, int] | None = None) -> None:
        raise NotImplementedError

    def add_card(self, title: str) -> Card:
        raise NotImplementedError

    def cards(self, column: str) -> list[Card]:
        raise NotImplementedError

    def move_card(self, card_id: int, to_column: str) -> None:
        raise NotImplementedError
'''

TEST_HEADER = "import pytest\n\nfrom board import Board, Card, UnknownCard, UnknownColumn, WipLimitExceeded\n"

API = """\
Public API (module board.py), the only thing tests may use:
  Board(columns: list[str], wip_limits: dict[str, int] | None = None)
  Board.add_card(title: str) -> Card          # Card is a frozen dataclass: id: int, title: str
  Board.cards(column: str) -> list[Card]
  Board.move_card(card_id: int, to_column: str) -> None
  Errors: UnknownCard, UnknownColumn, WipLimitExceeded
"""

SLICES = [
    "add_card puts the new card in the board's first column.",
    "cards(column) returns that column's cards in the order they were added.",
    "move_card moves a card: it leaves its old column and appears in the target column.",
    "move_card raises UnknownCard for an id that does not exist, and UnknownColumn for a "
    "column that does not exist (both assertions may live in this one test).",
    "A column with a WIP limit that already holds that many cards rejects move_card into it "
    "with WipLimitExceeded, and the card stays in its original column.",
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(argv: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    # stdin is closed: `codex exec` hangs on an open stdin.
    return subprocess.run(
        argv, cwd=WORKDIR, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=TIMEOUT,
    )  # fmt: skip


def pytest_cmd() -> str:
    return f"{sys.executable} -m pytest -q --tb=short -p no:cacheprovider test_board.py"


def count(output: str) -> tuple[int, int]:
    """(passed, failed) from a pytest summary; errors count as failed."""
    passed = sum(int(n) for n in re.findall(r"(\d+) passed", output))
    failed = sum(int(n) for n in re.findall(r"(\d+) (?:failed|error)", output))
    return passed, failed


def local_pytest() -> tuple[int, int, str]:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    r = run(pytest_cmd().split(), env=env)
    return (*count(r.stdout + r.stderr), r.stdout[-1500:])


def count_tests() -> int:
    return len(re.findall(r"^def test_", TESTS.read_text(), re.M))


def codex(cfg, worker: str, prompt: str) -> str:
    argv = codex_argv(cfg, worker, prompt, sandbox="workspace-write", cd=WORKDIR)
    argv.insert(argv.index("--"), "--skip-git-repo-check")
    r = run(argv)
    if r.returncode != 0:
        raise RuntimeError(f"{worker} exited {r.returncode}: {r.stderr[-500:]}")
    return r.stdout


def claude(cfg, prompt: str) -> str:
    # Clean env (a session's CLAUDE_CODE_* variables hang the nested call). No Bash:
    # the implementer can read and edit files but cannot run anything.
    env = {"HOME": os.environ["HOME"], "PATH": os.environ["PATH"], **claude_env(cfg, IMPLEMENTER)}
    argv = [
        "claude", "-p", prompt, "--model", env["ANTHROPIC_MODEL"],
        "--permission-mode", "acceptEdits",
        "--allowedTools", "Read,Edit,Write", "--disallowedTools", "Bash",
    ]  # fmt: skip
    r = run(argv, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"{IMPLEMENTER} exited {r.returncode}: {r.stderr[-500:]}")
    return r.stdout


def gemini_run_tests(cfg) -> tuple[int, int]:
    """Gemini runs the tests; the driver cross-checks with its own run."""
    prompt = (
        "Run this exact shell command in the current directory. Do not modify any files.\n"
        f"PYTHONDONTWRITEBYTECODE=1 {pytest_cmd()}\n"
        "Reply with the pytest summary line, then as the very last line exactly "
        "`RESULT: passed=<N> failed=<M>` where N is the number of tests that passed and M the "
        "number that failed or errored."
    )
    out = codex(cfg, TEST_RUNNER, prompt)
    m = re.findall(r"RESULT:\s*passed=(\d+)\s+failed=(\d+)", out)
    if not m:
        raise RuntimeError(f"{TEST_RUNNER} gave no RESULT line:\n{out[-500:]}")
    reported = (int(m[-1][0]), int(m[-1][1]))
    passed, failed, tail = local_pytest()
    if reported != (passed, failed):
        raise RuntimeError(
            f"{TEST_RUNNER} reported passed/failed={reported} but pytest says "
            f"{(passed, failed)}:\n{tail}"
        )
    return reported


def step(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    cfg = load_config(ROOT / "llm.yaml")
    for w in (TEST_WRITER, IMPLEMENTER, TEST_RUNNER):
        try:
            cfg.check_request(w)
        except Exception as e:
            raise ConfigError(f"{w} must be enabled in llm.yaml: {e}") from e

    WORKDIR.mkdir(exist_ok=True)
    BOARD.write_text(SKELETON)
    TESTS.write_text(TEST_HEADER)
    (WORKDIR / "__init__.py").unlink(missing_ok=True)

    for i, behaviour in enumerate(SLICES, start=1):
        step(f"\n=== slice {i}/{len(SLICES)}: {behaviour}")

        # 1. RED: codex-gpt writes one test.
        # The worker returns the test as text and the driver appends it: codex's file-edit
        # tool timed out here while `codex exec` still exited 0, so edits can't be trusted.
        board_before, tests_before = sha(BOARD), TESTS.read_text()
        out = codex(
            cfg,
            TEST_WRITER,
            "You are writing ONE test for a Python kanban board library, test-first.\n"
            "Do not edit, create or run anything. Reply with exactly one ```python code block "
            "containing a single new pytest test function and nothing else (no imports, no "
            "fixtures, no other functions).\n"
            "Rules:\n"
            "- Test behavior through the public API only. No private attributes.\n"
            "- Use literal expected values; never recompute them the way the code would.\n"
            f"- The test name must start with test_ and be unique; {i - 1} tests already exist.\n"
            "- These names are already imported: Board, Card, UnknownCard, UnknownColumn, "
            "WipLimitExceeded, pytest. Card ids start at 1 and increase by 1.\n\n"
            f"{API}\nBehavior to test: {behaviour}\n",
        )
        blocks = re.findall(r"```python\n(.*?)```", out, re.S)
        if len(blocks) != 1 or blocks[0].count("def test_") != 1:
            raise RuntimeError(f"{TEST_WRITER} must return one test in one python block:\n{out[-600:]}")
        TESTS.write_text(tests_before.rstrip("\n") + "\n\n\n" + blocks[0].strip() + "\n")
        if sha(BOARD) != board_before:
            raise RuntimeError(f"{TEST_WRITER} modified board.py")
        step(f"  [{TEST_WRITER}] wrote test: {blocks[0].strip().splitlines()[0]}")

        total = count_tests()
        passed, failed = gemini_run_tests(cfg)
        step(f"  [{TEST_RUNNER}] RED check: passed={passed} failed={failed} (cross-checked)")
        if (passed, failed) != (total - 1, 1):
            if failed != 0 or passed != total:
                raise RuntimeError(f"unexpected test run: passed={passed} failed={failed}")
            # The test passed without new code, so the behaviour was already built by an
            # earlier minimal implementation. Ask for a different test once; if it still
            # passes, record the slice as already covered and move on.
            step(f"  [{TEST_WRITER}] new test passed without new code; asking for a failing one")
            tests_before = TESTS.read_text()
            out = codex(
                cfg,
                TEST_WRITER,
                "The test you just wrote passes on the current board.py, so it does not drive "
                "new code. Reply with exactly one ```python block containing ONE new test "
                f"function for this behaviour that is NOT yet implemented: {behaviour}\n"
                "Same rules as before: public API only, literal expected values, no other "
                f"functions, no imports, and the name must not repeat an existing test.\n\n{API}",
            )
            blocks = re.findall(r"```python\n(.*?)```", out, re.S)
            if len(blocks) == 1 and blocks[0].count("def test_") == 1:
                TESTS.write_text(tests_before.rstrip("\n") + "\n\n\n" + blocks[0].strip() + "\n")
                passed, failed = gemini_run_tests(cfg)
                step(f"  [{TEST_RUNNER}] RED check 2: passed={passed} failed={failed} (cross-checked)")
            total = count_tests()
            if (passed, failed) == (total, 0):
                step(f"  slice {i} already covered by the earlier minimal implementation; no red step")
                continue
            if (passed, failed) != (total - 1, 1):
                raise RuntimeError(f"expected exactly the new test to fail, got passed={passed} failed={failed}")

        # 2. GREEN: claude writes the minimum implementation, one retry with the failure.
        for attempt in (1, 2):
            tests_hash = sha(TESTS)
            feedback = "" if attempt == 1 else f"\nYour previous attempt failed:\n{local_pytest()[2]}\n"
            claude(
                cfg,
                "Make the failing test in test_board.py pass, test-first.\n"
                "Read test_board.py and board.py. Edit only board.py. Do not edit test_board.py. "
                "Write the minimum code the tests require; do not add features no test asks for. "
                "You cannot run the tests; reason about them.\n\n"
                f"{API}{feedback}",
            )
            if sha(TESTS) != tests_hash:
                raise RuntimeError(f"{IMPLEMENTER} modified test_board.py")
            passed, failed = gemini_run_tests(cfg)
            step(f"  [{IMPLEMENTER}] attempt {attempt} -> [{TEST_RUNNER}] GREEN check: "
                 f"passed={passed} failed={failed} (cross-checked)")  # fmt: skip
            if (passed, failed) == (total, 0):
                break
        else:
            raise RuntimeError(f"slice {i} did not go green after 2 attempts")

    step(f"\nDone: {len(SLICES)} slices, all green. Code in {BOARD.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
