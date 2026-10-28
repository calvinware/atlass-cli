"""Golden one-call scenarios: each task is a single invocation with a small, complete answer.

Writes (and their refusals) stay within 2 KiB of JSON and reads within 4 KiB. A refusal lists
every problem at once, sends nothing, and carries a shell-quoted corrected command; an uncertain
or partial write never offers one.
"""

from __future__ import annotations

import json
import shlex
from typing import Any

import httpx
import pytest

from tests.conftest import (
    BITBUCKET,
    BITBUCKET_TOKEN,
    CONFLUENCE_TOKEN,
    Harness,
    bitbucket_path,
    config_text,
    confluence_path,
    fixture,
    jira_path,
    run_argv,
)
from yojo.contracts import COMMANDS_BY_PATH

WRITE_BUDGET = 2 * 1024
READ_BUDGET = 4 * 1024

ISSUETYPES = jira_path("/rest/api/2/issue/createmeta/EXAMPLE/issuetypes")
BUG_FIELDS = jira_path("/rest/api/2/issue/createmeta/EXAMPLE/issuetypes/10002")
CREATE = jira_path("/rest/api/2/issue")
ISSUE = jira_path("/rest/api/2/issue/EXAMPLE-1")
NEW_ISSUE = jira_path("/rest/api/2/issue/EXAMPLE-42")
EDITMETA = jira_path("/rest/api/2/issue/EXAMPLE-1/editmeta")
TRANSITIONS = jira_path("/rest/api/2/issue/EXAMPLE-1/transitions")
PERMISSIONS = jira_path("/rest/api/2/mypermissions")
COMMENT = jira_path("/rest/api/2/issue/EXAMPLE-1/comment")
FIELDS = jira_path("/rest/api/2/field")
PAGE = confluence_path("/rest/api/content/123456")

CREATE_ARGV = [
    "jira",
    "issue",
    "create",
    "EXAMPLE",
    "--type",
    "Bug",
    "--summary",
    "Login button misaligned",
    "--set",
    "Severity=Major",
    "--set",
    "Environment=Firefox on Linux",
]


def jira(harness: Harness, config: str | None = None) -> None:
    harness.write_config(config or config_text())
    harness.store()


def issue(**fields: Any) -> dict[str, Any]:
    raw: dict[str, Any] = fixture("issue_todo.json")
    raw["fields"].update(fields)
    return raw


def setup_create(harness: Harness) -> None:
    jira(harness)
    harness.server.add("GET", ISSUETYPES, fixture("createmeta_issuetypes.json"))
    harness.server.add("GET", BUG_FIELDS, fixture("createmeta_bug_fields.json"))
    harness.server.add("POST", CREATE, {"id": "30001", "key": "EXAMPLE-42"}, status=201)
    harness.server.add("GET", NEW_ISSUE, issue())


def refusal(result: Any) -> dict[str, Any]:
    assert result.code == 4, result.out
    error = result.error
    assert error["code"] == "requirements_unmet"
    details: dict[str, Any] = error["details"]
    assert details["nothing_sent"] is True
    assert "nothing was sent" in error["message"]
    if details["retry_argv"] is not None:
        assert details["retry"] == shlex.join(details["retry_argv"])
        assert run_argv(details["retry_argv"]) == details["retry_argv"]
    return details


# --------------------------------------------------------------------------- golden writes


def test_golden_create(harness: Harness) -> None:
    setup_create(harness)
    result = harness.run(*CREATE_ARGV)
    assert result.code == 0
    assert result.data["key"] == "EXAMPLE-42"
    assert result.data["url"].endswith("/browse/EXAMPLE-42")
    assert len(harness.server.writes()) == 1
    assert len(harness.server.requests) == 4
    assert result.nbytes <= WRITE_BUDGET


def test_golden_update(harness: Harness) -> None:
    jira(harness)
    harness.server.add("GET", EDITMETA, fixture("editmeta_bug.json"))
    harness.server.add("PUT", ISSUE, status=204)
    harness.server.add("GET", ISSUE, issue(priority={"id": "2", "name": "High"}))
    result = harness.run("jira", "issue", "update", "EXAMPLE-1", "--set", "priority=high")
    assert result.code == 0
    assert result.data["changed"] == [{"field": "Priority", "id": "priority"}]
    assert len(harness.server.writes()) == 1
    assert result.nbytes <= WRITE_BUDGET


def test_golden_transition(harness: Harness) -> None:
    jira(harness)
    harness.server.add("GET", ISSUE, issue())
    harness.server.add("GET", TRANSITIONS, fixture("transitions_todo.json"))
    harness.server.add("POST", TRANSITIONS, status=204)
    result = harness.run("jira", "transition", "EXAMPLE-1", "--to", "in progress")
    assert result.code == 0
    assert result.data["status"] == "In Progress"
    assert result.data["changed"] == [{"field": "Review Flag", "id": "customfield_10300"}]
    assert len(harness.server.writes()) == 1
    assert result.nbytes <= WRITE_BUDGET


def test_golden_comment(harness: Harness) -> None:
    jira(harness)
    harness.server.add("GET", ISSUE, issue())
    harness.server.add(
        "GET", PERMISSIONS, {"permissions": {"ADD_COMMENTS": {"havePermission": True}}}
    )
    harness.server.add("POST", COMMENT, {"id": "777"}, status=201)
    result = harness.run("jira", "comment", "add", "EXAMPLE-1", "--body", "Ready for review.")
    assert result.code == 0
    assert result.data["url"].endswith("focusedCommentId=777#comment-777")
    assert result.nbytes <= WRITE_BUDGET


def test_golden_open_pull_request(harness: Harness) -> None:
    """The documented example, run as-is: internal preflight GETs, then exactly one POST."""
    example = shlex.split(COMMANDS_BY_PATH["bitbucket pr create"].example)
    assert example[:2] == ["yojo", "--json"]
    harness.store("bitbucket", BITBUCKET_TOKEN)
    repo = bitbucket_path("/projects/PROJ/repos/web")
    branches = fixture("bitbucket_branches.json")
    pr = fixture("bitbucket_pull_request.json")
    harness.server.add("GET", repo, fixture("bitbucket_repo.json"))
    harness.server.add("GET", repo + "/branches", branches)
    harness.server.add(
        "GET", repo + "/pull-requests", {"size": 0, "isLastPage": True, "start": 0, "values": []}
    )
    harness.server.add("POST", repo + "/pull-requests", pr, status=201)
    result = harness.run(*example[1:], "--base-url", BITBUCKET)
    assert result.code == 0, result.out
    assert result.data["id"] == 42
    assert result.data["url"].endswith("/projects/PROJ/repos/web/pull-requests/42")
    assert [r.method for r in harness.server.requests] == ["GET", "GET", "GET", "GET", "POST"]
    assert result.nbytes <= WRITE_BUDGET


# --------------------------------------------------------------------------- golden reads


@pytest.mark.parametrize("body", ["<p>" + "x" * 20_000 + "</p>", "<p>" + "é中" * 5_000])
def test_golden_page_read_is_bounded(harness: Harness, body: str) -> None:
    harness.write_config()
    harness.store("confluence", CONFLUENCE_TOKEN)
    page = fixture("confluence_page.json")
    page["body"]["storage"]["value"] = body
    harness.server.add("GET", PAGE, page)
    result = harness.run("confluence", "page", "read", "123456")
    assert result.code == 0
    data = result.data
    assert data["truncated"] is True
    assert data["body_chars"] == len(body)
    assert body.startswith(data["body_storage"])
    assert data["more"] == "yojo confluence page read 123456 --full"
    assert result.nbytes <= READ_BUDGET
    full = harness.run("confluence", "page", "read", "123456", "--full")
    assert full.data["body_storage"] == body
    assert full.data["truncated"] is False


def test_golden_issue_get_with_several_long_fields_is_bounded(harness: Harness) -> None:
    jira(harness)
    harness.server.add("GET", FIELDS, fixture("fields.json"))
    harness.server.add(
        "GET",
        ISSUE,
        issue(
            description="é" * 3000,
            customfield_10600="y" * 3000,
            customfield_10700="z" * 3000,
            labels=[f"label-{n}" for n in range(200)],
        ),
    )
    names = "description,Environment,Component Owner,labels"
    result = harness.run("jira", "issue", "get", "EXAMPLE-1", "--fields", names)
    assert result.code == 0
    assert result.nbytes <= READ_BUDGET
    trimmed = [f["field"] for f in result.data["fields"] if f["truncated"]]
    assert trimmed
    assert result.data["more"].endswith("--full")


# --------------------------------------------------------------------------- refusals


def test_two_missing_fields_in_one_refusal(harness: Harness) -> None:
    setup_create(harness)
    result = harness.run(*CREATE_ARGV[:8])
    details = refusal(result)
    assert {c["id"] for c in details["checks"]} == {
        "required:customfield_10500",
        "required:customfield_10600",
    }
    assert details["retry_argv"][-4:] == [
        "--set",
        "Severity=<value>",
        "--set",
        "Environment=<value>",
    ]
    assert harness.server.writes() == []
    assert result.nbytes <= WRITE_BUDGET


def test_ambiguous_name_lists_candidates(harness: Harness) -> None:
    setup_create(harness)
    details = refusal(harness.run(*CREATE_ARGV, "--set", "component OWNER=me"))
    (check,) = details["checks"]
    assert check["details"]["kind"] == "ambiguous"
    assert sorted(check["details"]["candidates"]) == ["customfield_10700", "customfield_10701"]
    assert "component OWNER=<customfield_10700|customfield_10701>=me" not in details["retry"]
    assert "<customfield_10700|customfield_10701>=me" in details["retry_argv"]
    assert harness.server.writes() == []


def test_misspelling_gets_a_suggestion_and_a_corrected_retry(harness: Harness) -> None:
    setup_create(harness)
    argv = list(CREATE_ARGV)
    argv[argv.index("Severity=Major")] = "Severty=Major"
    details = refusal(harness.run(*argv))
    checks = {c["id"]: c for c in details["checks"]}
    assert checks["field:Severty"]["details"]["suggestion"] == "Severity"
    # The misspelled name did not supply Severity, so Jira's requirement is reported too.
    assert "required:customfield_10500" in checks
    assert details["retry_argv"] == ["yojo", *CREATE_ARGV]
    assert harness.server.writes() == []
    assert harness.run(*details["retry_argv"][1:]).code == 0


def test_refusal_sends_nothing_even_with_many_problems(harness: Harness) -> None:
    setup_create(harness)
    result = harness.run(
        "jira",
        "issue",
        "create",
        "EXAMPLE",
        "--type",
        "Bug",
        "--set",
        "Severity=Huge",
        "--set",
        "Nope=1",
        "--set",
        "Story Points=many",
    )
    details = refusal(result)
    # summary, three bad --set values, and Environment; Severity was given, so not "missing".
    assert [c["id"] for c in details["checks"]] == [
        "summary",
        "field:Severity",
        "field:Nope",
        "field:Story Points",
        "required:customfield_10600",
    ]
    assert harness.server.writes() == []
    assert result.nbytes <= WRITE_BUDGET


# --------------------------------------------------------------------------- uncertain writes


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


def test_uncertain_create_offers_inspection_not_a_retry(harness: Harness) -> None:
    setup_create(harness)
    harness.server.routes[("POST", CREATE)] = []
    harness.server.add("POST", CREATE, handler=_timeout)
    result = harness.run(*CREATE_ARGV)
    assert result.code == 6
    error = result.error
    assert error["code"] == "outcome_unknown"
    assert "retry" not in error["details"] and "retry_argv" not in error["details"]
    assert "Do not retry yet" in error["fix"]
    assert len(harness.server.writes()) == 1
    assert result.nbytes <= WRITE_BUDGET


def test_pre_edit_partial_offers_inspection_not_a_retry(harness: Harness) -> None:
    jira(harness, config_text(pre_edit="true"))
    harness.server.add("GET", ISSUE, issue())
    harness.server.add("GET", TRANSITIONS, fixture("transitions_todo_no_screen.json"))
    harness.server.add("GET", EDITMETA, fixture("editmeta.json"))
    harness.server.add("PUT", ISSUE, status=204)
    harness.server.add(
        "POST", TRANSITIONS, {"errorMessages": ["Validator failed"], "errors": {}}, status=400
    )
    result = harness.run("jira", "transition", "EXAMPLE-1", "--to", "In Progress")
    assert result.code == 7
    error = result.error
    assert error["code"] == "partial_transition"
    assert "retry" not in error["details"]
    assert set(error["details"]["recovery_argv"]) == {"inspect", "requirements"}
    assert [r.method for r in harness.server.writes()] == ["PUT", "POST"]
    assert result.nbytes <= WRITE_BUDGET


def test_compact_json_is_one_line(harness: Harness) -> None:
    setup_create(harness)
    result = harness.run(*CREATE_ARGV)
    assert result.stdout.count("\n") == 1
    assert json.loads(result.stdout)["ok"] is True
