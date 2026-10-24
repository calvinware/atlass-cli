from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from atlass_cli import cli
from atlass_cli.contracts import COMMANDS, COMMANDS_BY_PATH, ERROR_CODES, ExitCode, command_schema
from tests.conftest import Harness


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
        "jira story create",
        "jira requirements",
        "jira transition",
        "jira feature find",
        "jira feature create",
        "jira attachment add",
        "jira comment add",
        "jira search",
        "confluence page read",
        "confluence page search",
        "confluence page create",
        "confluence page update",
    ):
        assert expected in COMMANDS_BY_PATH


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
    assert not (harness.tmp_path / "atl.yaml").exists()


def test_schema_accepts_unquoted_words(harness: Harness) -> None:
    assert harness.run("schema", "jira", "story", "create").data["command"] == "jira story create"


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
        ["jira", "story", "create", "--help"],
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


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["bogus"],
        ["jira"],
        ["jira", "search"],
        ["jira", "requirements", "not-a-key", "--to", "Done"],
        ["jira", "search", "--jql", "x", "--limit", "101"],
        ["jira", "search", "--jql", "x", "--limit", "abc"],
        ["jira", "feature", "find"],
        ["jira", "feature", "find", "--issue", "EXAMPLE-1", "--text", "x"],
        ["confluence", "page", "read", "abc"],
        ["login", "--service", "jira", "--token", "secret-in-argv"],
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
        "fields": {"work_started": {"v": SECRET_SENTINEL}},
        "unexpected": SECRET_SENTINEL,
    }
    result = harness.run("jira", "story", "create", "--input", "-", stdin=json.dumps(doc).encode())
    assert result.error["code"] == "input_invalid"
    assert SECRET_SENTINEL not in result.stdout
    assert result.stderr == ""
    problems = {p["path"]: p for p in result.error["details"]["errors"]}
    assert problems["/summary"] == {"path": "/summary", "validator": "type", "expected": "string"}
    assert problems["/project"]["validator"] == "pattern"
    assert problems["/fields/work_started"]["validator"] == "oneOf"
    assert "summary" in problems["/"]["allowed"]

    doc["fields"] = {SECRET_SENTINEL: {"v": "invalid"}}
    result = harness.run("jira", "story", "create", "--input", "-", stdin=json.dumps(doc).encode())
    assert result.error["code"] == "input_invalid"
    assert SECRET_SENTINEL not in result.stdout
    assert "/fields/<field>" in {problem["path"] for problem in result.error["details"]["errors"]}

    result = harness.run("jira", "story", "create", "--input", "-", stdin=b"{}")
    assert result.error["details"]["errors"][0]["missing"] == ["project", "summary"]

    duplicate = json.dumps({"project": "EXAMPLE", "summary": "ok"})[:-1]
    duplicate += f', "{SECRET_SENTINEL}": 1, "{SECRET_SENTINEL}": 2}}'
    result = harness.run("jira", "story", "create", "--input", "-", stdin=duplicate.encode())
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


def test_config_not_found(harness: Harness) -> None:
    result = harness.run("jira", "search", "--jql", "project = X")
    assert result.code == 2
    assert result.error["code"] == "config_not_found"
    assert "atl.example.yaml" in result.error["fix"]


def test_input_rejects_duplicate_keys_and_schema_errors(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    dup = b'{"project": "EXAMPLE", "project": "OTHER", "summary": "x"}'
    result = harness.run("jira", "story", "create", "--input", "-", stdin=dup)
    assert result.error["code"] == "input_invalid"
    assert "duplicate" in result.error["message"]

    bad = json.dumps({"project": "EXAMPLE", "summary": "x", "unexpected": 1}).encode()
    result = harness.run("jira", "story", "create", "--input", "-", stdin=bad)
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
        [sys.executable, "-m", "atlass_cli", "jira", "search"],
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
