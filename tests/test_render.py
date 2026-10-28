from __future__ import annotations

import json
from typing import Any

import pytest

from tests.conftest import Harness, config_text, fixture, jira_path
from yojo.render import Style, clean, color_enabled, render

KEY = "EXAMPLE-1"
ISSUE = jira_path(f"/rest/api/2/issue/{KEY}")
TRANSITIONS = jira_path(f"/rest/api/2/issue/{KEY}/transitions")
SEARCH = jira_path("/rest/api/2/search")
ESC = "\x1b"


def setup_transition(harness: Harness, *, transitions: str = "transitions_todo.json") -> None:
    harness.write_config(config_text())
    harness.store()
    harness.server.add("GET", ISSUE, fixture("issue_todo.json"))
    harness.server.add("GET", TRANSITIONS, fixture(transitions))
    harness.server.add("POST", TRANSITIONS, status=204)


@pytest.mark.parametrize(
    "argv",
    [
        ["commands"],
        ["schema", "jira issue create"],
        ["bogus"],
        [],
        ["jira", "search", "--jql", "x"],
        ["jira", "transition", KEY, "--to", "In Progress"],
        ["jira", "transition", KEY, "--to", "Nowhere"],
    ],
)
def test_terminal_and_pipe_share_the_exit_code(harness: Harness, argv: list[str]) -> None:
    setup_transition(harness)
    harness.server.add("GET", SEARCH, {"total": 0, "issues": []})
    piped = harness.run(*argv)
    tty = harness.run_tty(*argv)
    assert tty.code == piped.code
    assert not tty.stdout.lstrip().startswith("{")


def test_json_flag_forces_compact_json_on_a_terminal(harness: Harness) -> None:
    tty = harness.run_tty("--json", "commands")
    assert tty.code == 0
    (line,) = tty.stdout.splitlines()
    assert json.loads(line)["ok"] is True
    assert ESC not in tty.stdout
    trailing = harness.run_tty("commands", "--json")
    assert json.loads(trailing.stdout)["ok"] is True


def test_pipe_output_has_no_color_even_when_the_terminal_would(harness: Harness) -> None:
    result = harness.run("bogus")
    assert ESC not in result.stdout + result.stderr


def test_terminal_uses_color_unless_no_color(harness: Harness) -> None:
    colored = harness.run_tty("bogus", environ={"TERM": "xterm-256color"})
    assert ESC in colored.stdout
    for environ in ({"NO_COLOR": "1"}, {"TERM": "dumb"}):
        plain = harness.run_tty("bogus", environ=environ)
        assert ESC not in plain.stdout
        assert plain.code == colored.code


def test_bare_yojo_on_a_terminal_shows_how_to_start(harness: Harness) -> None:
    # With no yojo.yaml this would be setup's JSON refusal: stdin here is not a terminal.
    harness.write_config()
    tty = harness.run_tty(environ={"NO_COLOR": "1"})
    assert tty.code == 2
    assert tty.stdout.startswith("Error: ")
    assert "yojo commands" in tty.stdout


def test_terminal_write_announces_intent_on_stderr_before_sending(harness: Harness) -> None:
    setup_transition(harness)
    tty = harness.run_tty(
        "jira", "transition", KEY, "--to", "In Progress", environ={"NO_COLOR": "1"}
    )
    assert tty.code == 0
    assert tty.stderr.startswith("Sending: Moving EXAMPLE-1 from 'To Do' to 'In Progress'")
    assert "Review Flag" in tty.stderr
    assert tty.stdout.startswith("Moved EXAMPLE-1")
    assert "Open: https://jira.example.test/jira/browse/EXAMPLE-1" in tty.stdout
    piped = harness.run("jira", "transition", KEY, "--to", "In Progress")
    assert piped.stderr == ""


def test_refusal_on_a_terminal_says_nothing_was_sent_and_how_to_retry(harness: Harness) -> None:
    setup_transition(harness)
    tty = harness.run_tty(
        "jira", "transition", KEY, "--to", "In Progres", environ={"NO_COLOR": "1"}
    )
    assert tty.code == 4
    assert "nothing was sent" in tty.stdout
    assert "Use --to 'In Progress'." in tty.stdout
    assert "Retry: yojo jira transition EXAMPLE-1 --to 'In Progress'" in tty.stdout
    assert tty.stderr == ""
    assert harness.server.writes() == []


def test_search_table_and_cursor_on_a_terminal(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    harness.server.add(
        "GET",
        SEARCH,
        {
            "total": 3,
            "issues": [
                {
                    "key": "EXAMPLE-1",
                    "fields": {"summary": "a", "status": {"name": "To Do"}},
                },
            ],
        },
    )
    tty = harness.run_tty("jira", "search", "--jql", "x", "--limit", "1", environ={"NO_COLOR": "1"})
    lines = tty.stdout.splitlines()
    assert any(line.startswith("Key") and "Status" in line for line in lines)
    assert any(line.startswith("EXAMPLE-1") for line in lines)
    assert "Next page: run the same command with --start 1" in tty.stdout


def test_empty_search_says_so(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    harness.server.add("GET", SEARCH, {"total": 0, "issues": []})
    tty = harness.run_tty("jira", "search", "--jql", "x", environ={"NO_COLOR": "1"})
    assert "No issues matched this search." in tty.stdout


def test_remote_text_cannot_inject_terminal_controls() -> None:
    envelope: dict[str, Any] = {
        "ok": True,
        "data": {
            "issues": [{"key": "EXAMPLE-1", "summary": "evil \x1b[2J\x1b]0;title\x07 text"}],
        },
    }
    out = render(envelope, "jira search", Style(color=False))
    assert ESC not in out and "\x07" not in out
    assert "evil" in out


def test_clean_bounds_long_values() -> None:
    assert len(clean("x" * 10_000)) <= 400
    assert clean(None) == "-"
    assert clean(True) == "yes"
    assert clean(["a", "b"]) == "a, b"


def test_color_enabled() -> None:
    assert color_enabled({}) is True
    assert color_enabled({"NO_COLOR": ""}) is True
    assert color_enabled({"NO_COLOR": "1"}) is False
    assert color_enabled({"TERM": "dumb"}) is False


def _diff_envelope(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": 42,
        "repo": "PROJ/web",
        "path": None,
        "files_total": 1,
        "files": [
            {"path": "a.py", "old_path": None, "change": "modified", "added": 1, "removed": 1}
        ],
        "diff": "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-old\n+new\n",
        "diff_chars": 60,
        "truncated": False,
        "server_truncated": False,
        "truncated_files": [],
        "complete": True,
        "more": None,
        "url": "https://bitbucket.example.test/bitbucket/projects/PROJ/repos/web/pull-requests/42/diff",
        "content_trust": "untrusted",
    }
    data.update(overrides)
    return {"ok": True, "data": data}


def test_diff_on_a_terminal_colors_changes_and_keeps_lines() -> None:
    out = render(_diff_envelope(), "bitbucket pr diff", Style(color=True))
    assert out.startswith("\x1b[1mPull request #42 in PROJ/web")
    assert "\x1b[31m-old\x1b[0m" in out and "\x1b[32m+new\x1b[0m" in out
    assert "--- a/a.py" in out and "Incomplete" not in out
    plain = render(_diff_envelope(), "bitbucket pr diff", Style(color=False))
    assert ESC not in plain
    assert "\n-old\n+new\n" in plain


def test_incomplete_diff_says_so_and_how_to_get_more() -> None:
    more = "yojo bitbucket pr diff 42 --repo PROJ/web --full"
    out = render(
        _diff_envelope(truncated=True, complete=False, more=more, diff="evil \x1b[2J line\n"),
        "bitbucket pr diff",
        Style(color=False),
    )
    assert "Incomplete: yojo shortened this diff." in out
    assert f"More: {more}" in out
    assert ESC not in out
