from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from tests.conftest import Harness
from yojo import cli
from yojo.contracts import (
    BITBUCKET_PROJECT_PATTERN,
    COMMANDS,
    COMMANDS_BY_PATH,
    ERROR_CODES,
    ISSUE_KEY_PATTERN,
    PAGE_ID_PATTERN,
    PROJECT_KEY_PATTERN,
    REPO_PATTERN,
    SPACE_KEY_PATTERN,
    ExitCode,
    command_schema,
)


def _leaf_paths(parser: argparse.ArgumentParser, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                path = f"{prefix} {name}".strip()
                children = _leaf_paths(sub, path)
                paths |= children or {path}
    return paths


def test_registry_parser_and_handlers_match() -> None:
    registry = {c.path for c in COMMANDS}
    assert _leaf_paths(cli.build_parser()) == registry
    assert set(cli.HANDLERS) == registry


def test_commands_lists_every_command_offline(harness: Harness) -> None:
    result = harness.run("commands")
    assert result.code == 0
    assert [c["path"] for c in result.data["commands"]] == [c.path for c in COMMANDS]
    assert harness.server.requests == []
    for expected in (
        "login",
        "config check",
        "init",
        "agent print",
        "agent install",
        "jira issue create",
        "jira issue get",
        "jira issue update",
        "jira issue link",
        "jira requirements",
        "jira transition",
        "jira attachment add",
        "jira comment add",
        "jira search",
        "confluence page read",
        "confluence page search",
        "confluence page create",
        "confluence page update",
        "bitbucket repo list",
        "bitbucket repo get",
        "bitbucket branch list",
        "bitbucket pr list",
        "bitbucket pr get",
        "bitbucket pr diff",
        "bitbucket pr create",
        "bitbucket pr comment",
    ):
        assert expected in COMMANDS_BY_PATH
    assert len(result.data["commands"]) == len(COMMANDS_BY_PATH)
    for command in result.data["commands"]:
        assert command["usage"].startswith("yojo ")


@pytest.mark.parametrize("path", [c.path for c in COMMANDS])
def test_schema_is_valid_and_offline(harness: Harness, path: str) -> None:
    result = harness.run("schema", path)
    assert result.code == 0
    data = result.data
    assert data["schema_version"] == 1
    assert data["command"] == path
    for key in ("arguments", "success", "error"):
        Draft202012Validator.check_schema(data[key])
    if data["input"] is not None:
        Draft202012Validator.check_schema(data["input"])
    assert set(data["error_codes"]) <= set(ERROR_CODES)
    assert harness.server.requests == []
    assert not (harness.tmp_path / "yojo.yaml").exists()
    assert data["usage"].startswith("yojo ") and data["example"].startswith("yojo ")


def test_schema_accepts_unquoted_words(harness: Harness) -> None:
    assert harness.run("schema", "jira", "issue", "create").data["command"] == "jira issue create"


def test_schema_unknown_command(harness: Harness) -> None:
    result = harness.run("schema", "jira", "delete")
    assert result.code == 2
    assert result.error["code"] == "usage_error"


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["-h"],
        ["jira", "--help"],
        ["jira", "issue", "create", "--help"],
        ["--config", "x", "login", "-h"],
    ],
)
def test_help_is_json(harness: Harness, argv: list[str]) -> None:
    result = harness.run(*argv)
    assert result.code == 0
    assert result.data["help"]["commands"]


def test_help_for_leaf_has_summary(harness: Harness) -> None:
    help_ = harness.run("jira", "transition", "--help").data["help"]
    assert help_["path"] == "jira transition"
    assert help_["summary"] == COMMANDS_BY_PATH["jira transition"].summary


def test_bare_yojo_returns_a_usage_envelope(harness: Harness) -> None:
    result = harness.run()
    assert result.code == ExitCode.INPUT
    assert result.error["code"] == "usage_error"
    assert "yojo commands" in result.error["fix"]
    assert harness.server.requests == []


@pytest.mark.parametrize(
    "argv",
    [
        ["bogus"],
        ["jira"],
        ["jira", "search"],
        ["jira", "requirements", "not-a-key", "--to", "Done"],
        ["jira", "search", "--jql", "x", "--limit", "101"],
        ["jira", "search", "--jql", "x", "--limit", "abc"],
        ["jira", "widget", "create"],
        ["jira", "widget", "find", "--text", "x"],
        ["jira", "issue", "link", "EXAMPLE-1", "EXAMPLE-2"],
        ["jira", "issue", "create", "EXAMPLE", "--set", "no-equals-sign"],
        ["jira", "comment", "add", "EXAMPLE-1"],
        ["jira", "comment", "add", "EXAMPLE-1", "--body", "x", "--body-file", "-"],
        ["confluence", "page", "read", "abc"],
        ["login", "--service", "jira", "--token", "secret-in-argv"],
        ["init", "--token", "secret-in-argv"],
        ["agent", "install", "--target", "notepad"],
        ["agent", "print", "--target", "agents"],
    ],
)
def test_parser_errors_are_json_exit_2(harness: Harness, argv: list[str]) -> None:
    result = harness.run(*argv)
    assert result.code == ExitCode.INPUT
    assert result.error["code"] == "usage_error"
    assert result.error["fix"]
    assert result.stderr == ""


SECRET_SENTINEL = "s3cr3t-SENTINEL-value"


@pytest.mark.parametrize(
    "argv",
    [
        ["login", "--service", "jira", "--token", SECRET_SENTINEL],
        ["login", "--service", "jira", f"--token={SECRET_SENTINEL}"],
        ["login", "--service", SECRET_SENTINEL],
        ["jira", "search", "--jql", "x", "--limit", SECRET_SENTINEL],
        ["jira", "requirements", SECRET_SENTINEL, "--to", "Done"],
        ["jira", "issue", "create", "EXAMPLE", "--set", SECRET_SENTINEL],
        [SECRET_SENTINEL],
    ],
)
def test_rejected_argument_values_are_not_echoed(harness: Harness, argv: list[str]) -> None:
    result = harness.run(*argv)
    assert result.code == 2
    assert result.error["code"] == "usage_error"
    assert SECRET_SENTINEL not in result.stdout
    assert result.stderr == ""


def test_schema_errors_omit_rejected_values(harness: Harness) -> None:
    harness.write_config()
    doc = {
        "project": SECRET_SENTINEL,
        "summary": ["x"],
        "fields": {"review_flag": {"v": SECRET_SENTINEL}},
        "unexpected": SECRET_SENTINEL,
    }
    result = harness.run("jira", "issue", "create", "--input", "-", stdin=json.dumps(doc).encode())
    assert result.error["code"] == "input_invalid"
    assert SECRET_SENTINEL not in result.stdout
    assert result.stderr == ""
    problems = {p["path"]: p for p in result.error["details"]["errors"]}
    assert problems["/summary"] == {"path": "/summary", "validator": "type", "expected": "string"}
    assert problems["/project"]["validator"] == "pattern"
    assert problems["/fields/<field>"]["validator"] == "oneOf"
    assert "summary" in problems["/"]["allowed"]

    doc["fields"] = {SECRET_SENTINEL: {"v": "invalid"}}
    result = harness.run("jira", "issue", "create", "--input", "-", stdin=json.dumps(doc).encode())
    assert result.error["code"] == "input_invalid"
    assert SECRET_SENTINEL not in result.stdout
    assert "/fields/<field>" in {problem["path"] for problem in result.error["details"]["errors"]}

    result = harness.run("jira", "issue", "create", "--input", "-", stdin=b"[]")
    assert result.error["details"]["errors"][0]["expected"] == "object"

    duplicate = json.dumps({"project": "EXAMPLE", "summary": "ok"})[:-1]
    duplicate += f', "{SECRET_SENTINEL}": 1, "{SECRET_SENTINEL}": 2}}'
    result = harness.run("jira", "issue", "create", "--input", "-", stdin=duplicate.encode())
    assert result.error["code"] == "input_invalid"
    assert SECRET_SENTINEL not in result.stdout


def test_internal_error_is_sanitized(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(args: Any, rt: Any) -> Any:
        raise RuntimeError("password=hunter2 at /secret/path")

    monkeypatch.setitem(cli.HANDLERS, "commands", boom)
    result = harness.run("commands")
    assert result.code == ExitCode.TRANSPORT
    assert result.error["code"] == "internal_error"
    assert "hunter2" not in result.stdout
    assert result.stderr == ""


def test_keyboard_interrupt(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupt(args: Any, rt: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setitem(cli.HANDLERS, "commands", interrupt)
    result = harness.run("commands")
    assert result.error["code"] == "interrupted"
    assert result.code == 6


def test_exit_code_catalog() -> None:
    assert {int(c) for c in ERROR_CODES.values()} == {2, 3, 4, 5, 6, 7}
    assert ERROR_CODES["partial_transition"] == 7
    assert ERROR_CODES["outcome_unknown"] == 6
    assert ERROR_CODES["requirements_unmet"] == 4


def test_every_schema_lists_universal_errors() -> None:
    for spec in COMMANDS:
        codes = command_schema(spec)["error_codes"]
        assert {"usage_error", "internal_error"} <= set(codes)


def test_no_config_and_no_base_url(harness: Harness) -> None:
    result = harness.run("jira", "search", "--jql", "project = X")
    assert result.code == 2
    assert result.error["code"] == "service_not_configured"
    assert "--base-url" in result.error["fix"] and "yojo.yaml" in result.error["fix"]


def test_explicit_missing_config_is_config_not_found(harness: Harness) -> None:
    result = harness.run("--config", "missing.yaml", "jira", "search", "--jql", "x")
    assert result.code == 2
    assert result.error["code"] == "config_not_found"


def test_create_without_project_is_refused_before_config(harness: Harness) -> None:
    result = harness.run("jira", "issue", "create", "--summary", "x")
    assert result.code == 2
    assert result.error["code"] == "usage_error"
    assert harness.server.requests == []


def test_flag_and_input_disagreeing_is_refused(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    doc = json.dumps({"project": "EXAMPLE", "summary": "from input"}).encode()
    result = harness.run(
        "jira", "issue", "create", "--summary", "from flag", "--input", "-", stdin=doc
    )
    assert result.error["code"] == "input_invalid"
    assert result.error["details"]["conflicts"] == ["summary"]
    assert harness.server.requests == []


def test_set_repeated_with_different_values_is_refused(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    result = harness.run(
        "jira", "issue", "update", "EXAMPLE-1", "--set", "Priority=High", "--set", "priority=Low"
    )
    assert result.error["code"] == "input_invalid"
    assert result.error["details"]["conflicts"] == ["priority"]
    assert harness.server.requests == []


def test_set_splits_at_the_first_equals_sign() -> None:
    args = cli.build_parser().parse_args(
        ["jira", "issue", "update", "EXAMPLE-1", "--set", "Environment=a=b", "--set=X= y "]
    )
    names = [(a.name, a.value) for a in cli._assignments(args, {})]
    assert names == [("Environment", "a=b"), ("X", " y ")]


def test_input_rejects_duplicate_keys_and_schema_errors(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    dup = b'{"project": "EXAMPLE", "project": "OTHER", "summary": "x"}'
    result = harness.run("jira", "issue", "create", "--input", "-", stdin=dup)
    assert result.error["code"] == "input_invalid"
    assert "duplicate" in result.error["message"]

    bad = json.dumps({"project": "EXAMPLE", "summary": "x", "unexpected": 1}).encode()
    result = harness.run("jira", "issue", "create", "--input", "-", stdin=bad)
    assert result.error["code"] == "input_invalid"
    assert result.error["details"]["errors"]
    assert harness.server.requests == []


def test_input_size_cap(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    harness.write_config()
    monkeypatch.setattr(cli, "MAX_INPUT_BYTES", 10)
    result = harness.run(
        "confluence", "page", "create", "--input", "-", stdin=b"{" + b" " * 20 + b"}"
    )
    assert result.error["code"] == "input_invalid"


def test_input_file_relative_to_cwd(harness: Harness) -> None:
    harness.write_config()
    (harness.tmp_path / "page.json").write_text('{"space_key": "ENG"}')
    result = harness.run("confluence", "page", "create", "--input", "page.json")
    assert result.error["code"] == "input_invalid"
    assert "/" in result.error["details"]["errors"][0]["path"]


def test_installed_entry_point_emits_single_json(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "yojo", "jira", "search"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 2
    assert len(proc.stdout.splitlines()) == 1
    assert json.loads(proc.stdout)["error"]["code"] == "usage_error"
    assert proc.stderr == ""


@pytest.mark.parametrize(
    "argv",
    [
        ["jira", "requirements", "EXAMPLE-1\n", "--to", "Done"],
        ["jira", "issue", "get", "EXAMPLE-1\n"],
        ["jira", "issue", "link", "EXAMPLE-1", "EXAMPLE-2\n", "--type", "blocks"],
        ["confluence", "page", "read", "123456\n"],
    ],
)
def test_key_and_id_arguments_reject_trailing_newline(harness: Harness, argv: list[str]) -> None:
    harness.write_config()
    harness.store()
    result = harness.run(*argv)
    assert result.code == ExitCode.INPUT
    assert result.error["code"] == "usage_error"
    assert harness.server.requests == []


@pytest.mark.parametrize(
    ("path", "doc"),
    [
        ("jira issue create", {"project": "EXAMPLE\n", "summary": "x"}),
        ("confluence page create", {"space_key": "ENG\n", "title": "T", "body_storage": ""}),
        (
            "confluence page create",
            {"space_key": "ENG", "title": "T", "body_storage": "", "parent_id": "1000\n"},
        ),
    ],
)
def test_input_schemas_reject_trailing_newline(
    harness: Harness, path: str, doc: dict[str, Any]
) -> None:
    harness.write_config()
    harness.store()
    harness.store("confluence")
    result = harness.run(*path.split(), "--input", "-", stdin=json.dumps(doc).encode())
    assert result.code == ExitCode.INPUT
    assert result.error["code"] == "input_invalid"
    assert harness.server.requests == []
    valid = {k: v.rstrip("\n") if isinstance(v, str) else v for k, v in doc.items()}
    input_schema = command_schema(COMMANDS_BY_PATH[path])["input"]
    Draft202012Validator(input_schema).validate(valid)


@pytest.mark.parametrize(
    ("pattern", "valid"),
    [
        (ISSUE_KEY_PATTERN, "ABC-1"),
        (PROJECT_KEY_PATTERN, "ABC"),
        (PAGE_ID_PATTERN, "123456"),
        (SPACE_KEY_PATTERN, "~user"),
        (REPO_PATTERN, "PROJ/web"),
        (BITBUCKET_PROJECT_PATTERN, "~example.user"),
    ],
)
def test_key_patterns_have_portable_absolute_end(pattern: str, valid: str) -> None:
    # `\Z` is Python-only; JSON Schema patterns must also work in ECMAScript validators.
    assert "\\Z" not in pattern and not pattern.endswith("$")
    assert re.fullmatch(pattern, valid)
    for bad in (valid + "\n", valid + "\r\n", valid + "\nX"):
        assert re.search(pattern, bad) is None
        assert not Draft202012Validator({"type": "string", "pattern": pattern}).is_valid(bad)
