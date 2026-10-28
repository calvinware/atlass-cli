"""`yojo agent print|install`: registry-derived instructions, size budget, and safe installs."""

from __future__ import annotations

import argparse
import math
import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.generate_command_reference import leaf_parsers
from tests.conftest import Harness
from yojo import agents, files
from yojo.cli import build_parser
from yojo.contracts import COMMANDS, COMMANDS_BY_PATH

ALL_TARGETS = list(agents.TARGETS)
GLOBAL_FLAGS = {"--config", "--base-url", "--json"}
AGENTS_MD = "# Team notes\n\nKeep tests green.\n"


def install(harness: Harness, *targets: str) -> Any:
    argv = ["agent", "install"]
    for target in targets:
        argv += ["--target", target]
    return harness.run(*argv)


def frontmatter(text: str) -> dict[str, Any]:
    assert text.startswith("---\n")
    head, sep, _ = text[4:].partition("\n---\n")
    assert sep
    loaded = yaml.safe_load(head)
    assert isinstance(loaded, dict)
    return loaded


# --------------------------------------------------------------------------- the core text


def test_print_returns_the_complete_core_offline(harness: Harness) -> None:
    result = harness.run("agent", "print")
    assert result.code == 0
    data = result.data
    assert data["text"] == agents.core()
    assert data["bytes"] == len(data["text"].encode("utf-8"))
    assert data["approx_tokens"] == math.ceil(len(data["text"]) / 4)
    assert data["commands"] == len(COMMANDS)
    assert harness.server.requests == []
    assert list(harness.tmp_path.iterdir()) == []


def test_every_command_appears_once_with_signature_and_json_example() -> None:
    text = agents.core()
    signatures = [line[3:-1] for line in text.splitlines() if line.startswith("- `yojo ")]
    assert signatures == [spec.usage for spec in COMMANDS]
    for spec in COMMANDS:
        assert text.count(f"- `{spec.usage}`\n") == 1, spec.path
        assert text.count(f"e.g. `{spec.example}`") == 1, spec.path
        if spec.input is not None:
            assert f"Input: `{agents.shape(spec.input)}`" in text


@pytest.mark.parametrize("path", list(COMMANDS_BY_PATH))
def test_signature_names_every_parser_flag_and_example_parses(path: str) -> None:
    spec = COMMANDS_BY_PATH[path]
    leaf = leaf_parsers(build_parser())[path]
    flags = {
        action.option_strings[0]
        for action in leaf._actions
        if action.option_strings and action.option_strings[0] not in GLOBAL_FLAGS | {"-h"}
    }
    for flag in flags:
        assert flag in spec.usage, f"{path}: {flag} missing from the signature"
    positionals = [a for a in leaf._actions if not a.option_strings]
    assert len(spec.usage.split()) > len(path.split()) + len(positionals) - 1
    argv = shlex.split(spec.example)
    assert argv[:2] == ["yojo", "--json"], path
    parsed: argparse.Namespace = build_parser().parse_args(argv[1:])
    assert parsed.command == path


def test_core_stands_alone_and_carries_the_safety_rules() -> None:
    text = " ".join(agents.core().split())
    assert "You do not need `yojo commands`, `yojo schema`, or `--help` first." in text
    for phrase in (
        "requirements_unmet",
        "details.retry",
        "outcome_unknown",
        "partial_transition",
        "details.recovery_argv",
        "OS keyring",
        "Only a human runs `yojo login`",
        "untrusted",
        "--set 'NAME=VALUE'",
        "--input -",
        "--base-url",
        "next_start",
        "Never approve or merge",
    ):
        assert phrase in text, phrase
    for word in ("Bearer", "Authorization", "password="):
        assert word not in text


@pytest.mark.parametrize("target", ALL_TARGETS)
def test_installed_text_fits_the_budget(
    target: str, record_property: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    text = agents.core()
    owned = agents.managed(agents.TARGETS[target], text)
    size = len(owned.encode("utf-8"))
    approx = agents.approx_tokens(owned)
    record_property("installed_bytes", size)
    record_property("approx_tokens_chars_div_4", approx)
    with capsys.disabled():
        print(f"\n{target}: {size} UTF-8 bytes, ~{approx} tokens (characters / 4)")
    assert size <= agents.CORE_BUDGET
    assert approx <= agents.CORE_BUDGET // 4


def test_shape_renders_nested_input() -> None:
    schema = COMMANDS_BY_PATH["confluence page create"].input
    assert schema is not None
    assert agents.shape(schema) == (
        "{space_key*: text, title*: text, body_storage*: text, parent_id: text}"
    )
    assert "transitions: {NAME: {from*: [text], to*: text" in agents.core()


# --------------------------------------------------------------------------- install


def test_every_target_installs_then_is_byte_stable(harness: Harness) -> None:
    first = install(harness, *ALL_TARGETS)
    assert first.code == 0, first.out
    rows = first.data["files"]
    assert [r["target"] for r in rows] == ALL_TARGETS
    assert {r["action"] for r in rows} == {"created"}
    core = agents.core()
    snapshots = {}
    for row in rows:
        path = harness.tmp_path / row["path"]
        text = path.read_text(encoding="utf-8")
        assert text == agents.managed(agents.TARGETS[row["target"]], core)
        assert row["managed_bytes"] == len(text.encode("utf-8")) <= agents.CORE_BUDGET
        snapshots[path] = (path.read_bytes(), path.stat().st_mtime_ns)
    second = install(harness, *ALL_TARGETS)
    assert {r["action"] for r in second.data["files"]} == {"unchanged"}
    for path, (data, mtime) in snapshots.items():
        assert path.read_bytes() == data
        assert path.stat().st_mtime_ns == mtime
    for directory in {p.parent for p in snapshots}:
        assert not [p for p in directory.iterdir() if p.name.endswith((".tmp", ".yojo-lock"))]


def test_target_wrappers_are_valid(harness: Harness) -> None:
    install(harness, "claude", "cursor")
    skill = frontmatter((harness.tmp_path / ".claude/skills/yojo/SKILL.md").read_text())
    assert skill["name"] == "yojo"
    assert 0 < len(skill["description"]) <= 1024
    rule = frontmatter((harness.tmp_path / ".cursor/rules/yojo.mdc").read_text())
    assert rule["alwaysApply"] is True
    assert rule["description"]


def test_repeated_target_is_installed_once(harness: Harness) -> None:
    result = install(harness, "agents", "agents")
    assert [r["target"] for r in result.data["files"]] == ["agents"]


@pytest.mark.parametrize(("target", "name"), [("agents", "AGENTS.md"), ("gemini", "GEMINI.md")])
def test_unrelated_text_is_kept_and_only_the_block_changes(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, target: str, name: str
) -> None:
    path = harness.tmp_path / name
    path.write_text(AGENTS_MD)
    assert install(harness, target).data["files"][0]["action"] == "updated"
    after = path.read_text()
    assert after.startswith(AGENTS_MD + "\n" + agents.BEGIN + "\n")
    path.write_text(after + "\n## Later notes\n\nStill mine.\n")
    monkeypatch.setattr(agents, "core", lambda commands=None: "## yojo\n\nNew text.\n")
    assert install(harness, target).data["files"][0]["action"] == "updated"
    assert path.read_text() == (
        AGENTS_MD
        + "\n"
        + agents.block("## yojo\n\nNew text.\n")
        + "\n## Later notes\n\nStill mine.\n"
    )


def test_existing_copilot_file_without_trailing_newline(harness: Harness) -> None:
    path = harness.tmp_path / ".github" / "copilot-instructions.md"
    path.parent.mkdir()
    path.write_text("Use tabs.")
    install(harness, "copilot")
    text = path.read_text()
    assert text.startswith("Use tabs.\n\n" + agents.BEGIN)
    install(harness, "copilot")
    assert path.read_text() == text


def test_crlf_files_keep_their_line_endings(harness: Harness) -> None:
    path = harness.tmp_path / "AGENTS.md"
    path.write_bytes(b"# Notes\r\n\r\nKeep.\r\n")
    install(harness, "agents")
    data = path.read_bytes()
    assert data.startswith(b"# Notes\r\n\r\nKeep.\r\n\r\n")
    assert b"\n" not in data.replace(b"\r\n", b"")
    install(harness, "agents")
    assert path.read_bytes() == data


def test_the_repository_agents_file_survives_an_install(harness: Harness) -> None:
    original = (Path(__file__).resolve().parent.parent / "AGENTS.md").read_bytes()
    path = harness.tmp_path / "AGENTS.md"
    path.write_bytes(original)
    install(harness, "agents")
    assert path.read_bytes().startswith(original)


@pytest.mark.parametrize(
    "text",
    [
        f"{agents.BEGIN}\nold\n{agents.END}\n{agents.BEGIN}\nold\n{agents.END}\n",
        f"{agents.END}\n{agents.BEGIN}\n",
        f"intro\n{agents.BEGIN}\nunfinished\n",
        f"intro\n{agents.END}\n",
        f"<!-- yojo:begin wrong marker -->\nold\n{agents.END}\n",
        f"{agents.BEGIN}\nold\n<!-- yojo:end wrong marker -->\n",
        f"intro {agents.BEGIN}\nold\n{agents.END}\n",
    ],
)
def test_malformed_markers_are_refused_and_nothing_is_written(harness: Harness, text: str) -> None:
    path = harness.tmp_path / "AGENTS.md"
    path.write_text(text)
    result = install(harness, "gemini", "agents")
    assert result.code == 2
    assert result.error["code"] == "file_conflict"
    assert "nothing was written" in result.error["message"]
    assert path.read_text() == text
    assert not (harness.tmp_path / "GEMINI.md").exists()


def test_a_concurrent_edit_is_refused(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    path = harness.tmp_path / "AGENTS.md"
    path.write_text(AGENTS_MD)
    real_stage = files._stage

    def racing_stage(dest: Path, data: bytes, mode: int) -> Path:
        staged = real_stage(dest, data, mode)
        dest.write_text("edited meanwhile\n")
        return staged

    monkeypatch.setattr(files, "_stage", racing_stage)
    result = install(harness, "agents")
    assert result.error["code"] == "file_conflict"
    assert path.read_text() == "edited meanwhile\n"
    assert sorted(p.name for p in harness.tmp_path.iterdir()) == ["AGENTS.md"]


def test_non_utf8_file_is_refused(harness: Harness) -> None:
    path = harness.tmp_path / "GEMINI.md"
    path.write_bytes(b"\xff\xfe notes")
    result = install(harness, "gemini")
    assert result.error["code"] == "file_invalid"
    assert path.read_bytes() == b"\xff\xfe notes"


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError:
        pytest.skip("symbolic links are not available")


def test_symlinked_file_or_folder_is_refused(harness: Harness) -> None:
    elsewhere = harness.tmp_path / "elsewhere"
    elsewhere.mkdir()
    _symlink(harness.tmp_path / "AGENTS.md", elsewhere / "AGENTS.md")
    assert install(harness, "agents").error["code"] == "file_invalid"
    _symlink(harness.tmp_path / ".claude", elsewhere)
    assert install(harness, "claude").error["code"] == "file_invalid"
    assert list(elsewhere.iterdir()) == []


def test_unknown_target_is_a_usage_error(harness: Harness) -> None:
    result = install(harness, "notepad")
    assert result.code == 2
    assert result.error["code"] == "usage_error"
    assert harness.run("agent", "install").error["code"] == "usage_error"


def test_terminal_views(harness: Harness) -> None:
    printed = harness.run_tty("agent", "print", environ={"NO_COLOR": "1"})
    assert printed.stdout.startswith("## yojo: Jira, Confluence, and Bitbucket Data Center\n")
    installed = harness.run_tty("agent", "install", "--target", "agents", environ={"NO_COLOR": "1"})
    assert installed.stdout.startswith("Installed yojo instructions for 1 tool(s).")
    assert "AGENTS.md" in installed.stdout
