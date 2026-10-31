"""The developer may amend the test file; the pipeline gate approves or reverts it (no LLM involved)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "sample"))

import pipeline  # noqa: E402
from pipeline import TEST_FILE, review_test_change  # noqa: E402
from pipeline_common import check_test_file  # noqa: E402

SPEC = {"acceptance_criteria": [{"id": "AC1", "text": "a"}, {"id": "AC2", "text": "b"}]}
BEFORE = "it('AC1: one', () => {})\nit('AC2: two', () => {})\n"
GOOD_EDIT = "it('AC1: one', () => {})\nit('AC2: two (fixed query)', () => {})\n"
DROPPED = "it('AC1: one', () => {})\n"


class FakeAgents:
    def __init__(self, approved):
        self.approved, self.calls = approved, []

    def send(self, stage, request):
        self.calls.append((stage, request))
        return {"approved": self.approved, "reason": "the tester's reason"}


class Log:
    def record(self, *args, **kwargs):
        pass


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(pipeline, "step", lambda msg: None)
    return tmp_path


def run_review(workdir, after, reason, approved=True):
    (workdir / TEST_FILE).write_text(after)
    agents = FakeAgents(approved)
    problem = review_test_change(agents, SPEC, workdir, BEFORE, reason, Log(), 0.0)
    return problem, agents, (workdir / TEST_FILE).read_text()


def test_approved_amendment_is_kept(workdir):
    problem, agents, text = run_review(workdir, GOOD_EDIT, "the query matched two elements")
    assert problem is None and text == GOOD_EDIT
    assert agents.calls[0][0] == "tests" and agents.calls[0][1]["mode"] == "review"


def test_rejected_amendment_is_reverted_and_reported(workdir):
    problem, _, text = run_review(workdir, GOOD_EDIT, "make it pass", approved=False)
    assert text == BEFORE
    assert "rejected your change" in problem and "restored" in problem


def test_amendment_without_reason_is_rejected_without_asking_the_tester(workdir):
    problem, agents, text = run_review(workdir, GOOD_EDIT, "")
    assert text == BEFORE and agents.calls == []
    assert "without a TEST CHANGE reason" in problem


def test_amendment_that_drops_a_criterion_is_rejected_without_asking_the_tester(workdir):
    problem, agents, text = run_review(workdir, DROPPED, "removed a flaky test")
    assert text == BEFORE and agents.calls == []
    assert "no test named for AC2" in problem


def test_check_test_file_checks():
    assert check_test_file(BEFORE, ["AC1", "AC2"]) is None
    assert check_test_file(DROPPED, ["AC1", "AC2"]) == "no test named for AC2"
    skipped = "it('AC1: x', () => {})\nit('AC2: y', () => {})\nit.skip('extra', () => {})"
    assert check_test_file(skipped, ["AC1", "AC2"]) == "the tests use .skip, .only or .todo"


def test_parameterised_tests_name_a_criterion():
    each = "it.each(['', '  '])('AC1: blank %s', () => {})\ntest.each([[1, 2]])('AC2: sums', () => {})\n"
    assert check_test_file(each, ["AC1", "AC2"]) is None
