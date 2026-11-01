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


# --- FR-27 to FR-29: content first, help[] lines, one example in each --help ---------------------


def test_no_command_prints_live_data_not_help(monkeypatch, tmp_path, capsys):
    (tmp_path / "a" / ".factory").mkdir(parents=True)
    (tmp_path / "a" / ".factory" / "run.json").write_text(
        json.dumps({"status": "accepted", "runbook": "research", "usage": {"cost_usd": 0.5}}))
    monkeypatch.setattr(cli, "APPS_DIR", tmp_path)
    assert cli.main([]) == 0
    got = capsys.readouterr().out
    assert got.startswith("bin: ") and "usage:" not in got
    assert "runs[1]{name,status,runbook,cost_usd}:\n  a,accepted,research,0.5" in got
    assert "runbooks[" in got and got.rstrip().splitlines()[-1].startswith("  - factory ")


def test_a_data_command_ends_with_help_lines_that_use_placeholders(capsys):
    assert cli.main(["runbooks"]) == 0
    tail = capsys.readouterr().out.split("help[")[1].splitlines()[1:]
    assert tail[0] == "  - factory plan <runbook>" and all(line.startswith("  - factory ") for line in tail)


def test_help_lines_are_in_json_too(capsys):
    assert cli.main(["runbooks", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["help"][0] == "factory plan <runbook>"


def test_runs_points_at_approve_for_a_run_that_waits(monkeypatch, tmp_path, capsys):
    (tmp_path / "a" / ".factory").mkdir(parents=True)
    (tmp_path / "a" / ".factory" / "run.json").write_text(json.dumps({"status": "awaiting", "runbook": "research"}))
    monkeypatch.setattr(cli, "APPS_DIR", tmp_path)
    cli.main(["runs"])
    assert "  - factory approve --name <run>" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["runbooks", "runs", "check", "plan", "run", "approve"])
def test_every_subcommand_help_has_one_example(command, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main([command, "--help"])
    got = capsys.readouterr().out
    assert e.value.code == 0 and got.count("example:\n  factory ") == 1


# --- FR-26: ambient context -----------------------------------------------------------------------


def test_setup_installs_the_session_hook_and_the_skill_and_a_second_run_changes_nothing(tmp_path, capsys):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
    assert cli.main(["setup", "--dir", str(tmp_path), "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    [hook] = settings["hooks"]["SessionStart"][0]["hooks"]
    assert hook["command"].endswith(" session") and settings["permissions"] == {"allow": ["Bash(ls)"]}
    skill = (tmp_path / ".claude" / "skills" / "factory" / "SKILL.md").read_text()
    assert skill.startswith("---\nname: factory\n") and "factory approve" in skill
    assert first["setup"] == "changed" and len(first["files"]) == 2
    before = (tmp_path / ".claude" / "settings.json").read_text()
    assert cli.main(["setup", "--dir", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["setup"] == "already done"
    assert (tmp_path / ".claude" / "settings.json").read_text() == before


def test_setup_refuses_a_settings_file_it_cannot_read_and_a_missing_folder(tmp_path, capsys):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{not json")
    assert cli.main(["setup", "--dir", str(tmp_path)]) == 1
    assert "not valid JSON" in capsys.readouterr().out
    assert (tmp_path / ".claude" / "settings.json").read_text() == "{not json"  # left as it was
    assert cli.main(["setup", "--dir", str(tmp_path / "nope")]) == 2


def test_session_prints_name_status_and_cost_and_points_at_a_waiting_run(monkeypatch, tmp_path, capsys):
    (tmp_path / "w" / ".factory").mkdir(parents=True)
    (tmp_path / "w" / ".factory" / "run.json").write_text(
        json.dumps({"status": "awaiting", "runbook": "research", "usage": {"cost_usd": 1.25}}))
    monkeypatch.setattr(cli, "APPS_DIR", tmp_path)
    assert cli.main(["session"]) == 0
    got = capsys.readouterr().out
    assert "runs[1]{name,status,cost_usd}:\n  w,awaiting,1.25" in got and "  - factory approve --name w" in got
