"""Roles are data: roles.yaml is validated when it loads, and a file can replace it."""

import pytest

from factory import ConfigError
from factory.roles import ROLES, ROLES_ENV, SEARCH, load_roles
from factory.runbook import catalog

ROLE = """
  scout:
    title: Scout
    description: Looks around.
    prompt: Look.
"""


def write(tmp_path, body):
    path = tmp_path / "roles.yaml"
    path.write_text("roles:" + body)
    return path


def test_the_shipped_roles_load():
    assert {"triage", "analyst", "architect", "tester", "developer", "verifier",
            "researcher", "auditor", "writer", "reviewer"} == set(ROLES)
    assert ROLES["developer"].edit and ROLES["developer"].runs == ("test", "build")
    assert ROLES["analyst"].asks and not ROLES["writer"].edit


def test_the_shipped_runbooks_load_against_the_shipped_roles():
    # Runbook validation refuses a step whose role is not in ROLES.
    assert {"web-app", "research", "code-audit", "api-design"} <= set(catalog())


def test_search_placeholder_is_filled():
    assert SEARCH in ROLES["researcher"].prompt and "{search}" not in ROLES["researcher"].prompt


def test_a_role_has_defaults(tmp_path):
    role = load_roles(write(tmp_path, ROLE))["scout"]
    assert (role.edit, role.web, role.asks, role.runs) == (False, False, False, ())


def test_the_roles_file_can_be_replaced_by_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(ROLES_ENV, str(write(tmp_path, ROLE)))
    assert set(load_roles()) == {"scout"}


@pytest.mark.parametrize("body, message", [
    (ROLE + "    sudo: true\n", "role 'scout', sudo"),  # a misspelt or unknown field
    (ROLE.replace("    title: Scout\n", ""), "role 'scout', title"),  # a missing field
    (ROLE + "    runs: [deploy]\n", "runs deploy"),  # a command that does not exist
    (ROLE.replace("scout", "Scout Two"), "must be lowercase"),  # a name the CLI cannot use
    (ROLE.replace("prompt: Look.", "prompt: ''"), "role 'scout', prompt"),  # an empty prompt
])
def test_a_bad_roles_file_is_refused_with_a_reason(tmp_path, body, message):
    with pytest.raises(ConfigError, match=message):
        load_roles(write(tmp_path, body))


@pytest.mark.parametrize("text, message", [
    ("", "expected a `roles:` mapping"),
    ("roles: []\n", "expected a `roles:` mapping"),
    ("roles: {}\n", "expected a `roles:` mapping"),
    ("roles: {a: [}\n", "not valid YAML"),
    ("roles:\n  scout: {title: a, description: b, prompt: c}\nextra: 1\n", "unknown top-level keys extra"),
])
def test_a_malformed_file_is_refused(tmp_path, text, message):
    path = tmp_path / "roles.yaml"
    path.write_text(text)
    with pytest.raises(ConfigError, match=message):
        load_roles(path)


def test_a_missing_file_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="cannot read the roles file"):
        load_roles(tmp_path / "nope.yaml")


def test_a_bad_roles_file_is_reported_by_the_command_not_at_import(tmp_path, monkeypatch, capsys):
    from factory.cli import main
    bad = tmp_path / "roles.yaml"
    bad.write_text("roles: {writer: {title: x}}\n")
    monkeypatch.setenv(ROLES_ENV, str(bad))
    monkeypatch.setattr("factory.roles.ROLES._roles", None)  # read the file again
    assert main(["runbooks"]) == 2
    assert "error: roles.yaml: role 'writer', description" in capsys.readouterr().err
