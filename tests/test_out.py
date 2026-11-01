"""AXI output (PRD FR-20 to FR-25): TOON by default, --json, --fields, --full, totals first,
definitive empty states and errors as data on stdout. No LLM."""

import json

import pytest

from factory import cli
from factory.out import clip, toon


def test_toon_names_the_keys_of_a_uniform_list_once():
    assert toon({"total": 2, "runbooks": [{"name": "a", "traits": "x y"}, {"name": "b", "traits": "none"}]}) == (
        "total: 2\nrunbooks[2]{name,traits}:\n  a,x y\n  b,none")


def test_toon_nests_objects_and_lists_scalars_inline():
    assert toon({"run": {"status": "accepted", "cost_usd": 0.5}, "tags": ["a", "b"], "none": []}) == (
        "run:\n  status: accepted\n  cost_usd: 0.5\ntags[2]: a,b\nnone[0]:")


@pytest.mark.parametrize("value, shown", [
    ("a, b", '"a, b"'), ("12", '"12"'), ("true", '"true"'), ("", '""'), ("x: y", '"x: y"'),
    ("line\nnext", '"line\\nnext"'), (None, "null"), (True, "true"), (0.1 + 0.2, "0.3"),
])
def test_toon_quotes_a_string_only_when_it_could_be_misread(value, shown):
    assert toon({"v": value}) == f"v: {shown}"


def test_a_list_of_mixed_rows_is_one_item_per_dash():
    assert toon({"x": [{"a": 1}, {"b": [1, 2]}]}) == "x[2]:\n  - a: 1\n  - b[2]: 1,2"


def test_long_text_is_cut_with_its_size_and_the_flag_that_shows_it():
    text = "word " * 100
    cut = clip(text)
    assert cut.endswith("(truncated, 499 chars in all; use --full)") and len(cut) < 260
    assert clip(text, full=True) == text.strip()


def test_runbooks_prints_the_total_first_and_three_fields(capsys):
    assert cli.main(["runbooks"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("total: ") and lines[1].endswith("{name,title,traits}:")


def test_fields_adds_a_field_and_json_prints_json(capsys):
    assert cli.main(["runbooks", "--json", "--fields", "needs"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["total"] == len(data["runbooks"]) and set(data["runbooks"][0]) == {"name", "title", "traits", "needs"}


def test_an_unknown_field_is_a_bad_command(capsys):
    assert cli.main(["runbooks", "--fields", "colour"]) == 2
    assert capsys.readouterr().out.startswith("error: \"--fields: no field colour")


def test_an_empty_list_says_so(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "APPS_DIR", tmp_path)
    assert cli.main(["runs"]) == 0
    assert capsys.readouterr().out.startswith("runs: 0 found")


def test_errors_are_data_on_stdout_and_stderr_stays_empty(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["runbooks", "--no-such-flag", "--json"])
    got = capsys.readouterr()
    assert e.value.code == 2 and "unrecognized arguments" in json.loads(got.out)["error"] and got.err == ""
