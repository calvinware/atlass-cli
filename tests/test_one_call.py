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
    CONFLUENCE,
    CONFLUENCE_TOKEN,
    JIRA_TOKEN,
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


# --------------------------------------------------------------------------- every command

# Each registered command's own example (the line `yojo agent install` writes) runs once, exactly
# as written, in a repository whose yojo.yaml names all three services. No `commands`, `schema`,
# `--help`, `requirements`, or retry call comes first.

GOLDEN_CONFIG = f"""\
version: 1
jira:
  base_url: https://jira.example.test/jira
confluence:
  base_url: {CONFLUENCE}
bitbucket:
  base_url: {BITBUCKET}
"""
OFFLINE = {"commands", "schema", "agent print", "agent install"}
# Opens a person's terminal app; an agent's --json call gets the step-by-step route instead.
TERMINAL_ONLY = {"setup"}
LOCAL_WRITES = {"agent install"}
REMOTE_WRITES = {
    "jira issue create",
    "jira issue update",
    "jira issue link",
    "jira transition",
    "jira attachment add",
    "jira comment add",
    "confluence page create",
    "confluence page update",
    "bitbucket pr create",
    "bitbucket pr comment",
}
# Discovery output, the agent text, and a config draft are not ordinary compact reads.
UNBUDGETED = {"commands", "schema", "agent print", "init"}
BB_REF = {"repository": {"slug": "web", "project": {"key": "PROJ"}}}
# The one write each representative example must send: method, path, query, and JSON body.
EXPECTED_WRITES: dict[str, tuple[str, str, dict[str, str], Any]] = {
    "jira issue create": (
        "POST",
        jira_path("/rest/api/2/issue"),
        {},
        {
            "fields": {
                "customfield_10002": 3,
                "priority": {"id": "2"},
                "project": {"key": "PROJ"},
                "issuetype": {"id": "10002"},
                "summary": "Login fails on Safari",
            }
        },
    ),
    "jira issue update": (
        "PUT",
        jira_path("/rest/api/2/issue/PROJ-123"),
        {},
        {"fields": {"customfield_10002": 5, "assignee": {"name": "example-user"}}},
    ),
    "bitbucket pr create": (
        "POST",
        bitbucket_path("/projects/PROJ/repos/web/pull-requests"),
        {},
        {
            "title": "Fix login on Safari",
            "fromRef": {"id": "refs/heads/feature/login", **BB_REF},
            "toRef": {"id": "refs/heads/main", **BB_REF},
        },
    ),
}
PROJ_123 = jira_path("/rest/api/2/issue/PROJ-123")
BB_REPO = bitbucket_path("/projects/PROJ/repos/web")
BB_PR = BB_REPO + "/pull-requests/42"


def proj_issue(key: str = "PROJ-123", **fields: Any) -> dict[str, Any]:
    raw: dict[str, Any] = fixture("issue_todo.json")
    raw["key"] = key
    raw["self"] = f"https://jira.example.test/jira/rest/api/2/issue/{key}"
    raw["fields"]["project"] = {"key": "PROJ", "id": "10000"}
    raw["fields"].update(fields)
    return raw


def bb_page(values: list[Any]) -> dict[str, Any]:
    return {"size": len(values), "limit": 25, "start": 0, "isLastPage": True, "values": values}


def _login(h: Harness) -> None:
    h.server.add("GET", jira_path("/rest/api/2/myself"), {"name": "example-user"})


def _config_check(h: Harness) -> None:
    h.server.add("GET", FIELDS, fixture("fields.json"))
    h.server.add(
        "GET",
        bitbucket_path("/application-properties"),
        fixture("bitbucket_application_properties.json"),
    )
    h.server.add("GET", bitbucket_path("/profile/recent/repos"), bb_page([]))


def _init(h: Harness) -> None:
    _login(h)
    h.server.add("GET", jira_path("/rest/api/2/project/PROJ"), fixture("jira_project.json"))


def _issue_create(h: Harness) -> None:
    fields = fixture("createmeta_bug_fields.json")
    for item in fields["values"]:
        if item["fieldId"] in ("customfield_10500", "customfield_10600"):
            item["required"] = False
    h.server.add(
        "GET",
        jira_path("/rest/api/2/issue/createmeta/PROJ/issuetypes"),
        fixture("createmeta_issuetypes.json"),
    )
    h.server.add("GET", jira_path("/rest/api/2/issue/createmeta/PROJ/issuetypes/10002"), fields)
    h.server.add("POST", CREATE, {"id": "30001", "key": "PROJ-124"}, status=201)
    h.server.add("GET", jira_path("/rest/api/2/issue/PROJ-124"), proj_issue("PROJ-124"))


def _issue_get(h: Harness) -> None:
    h.server.add("GET", FIELDS, fixture("fields.json"))
    h.server.add("GET", PROJ_123, proj_issue(customfield_10002=3, description="Steps"))


def _issue_update(h: Harness) -> None:
    person = {"name": "example-user", "key": "u1", "displayName": "Example User"}
    h.server.add("GET", PROJ_123 + "/editmeta", fixture("editmeta_bug.json"))
    h.server.add("GET", jira_path("/rest/api/2/user/search"), [person])
    h.server.add("PUT", PROJ_123, status=204)
    h.server.add("GET", PROJ_123, proj_issue(customfield_10002=5, assignee=person))


def _issue_link(h: Harness) -> None:
    proj_1 = jira_path("/rest/api/2/issue/PROJ-1")
    outward = {"id": "1", "type": {"id": "10000"}, "outwardIssue": {"key": "PROJ-2"}}
    h.server.add("GET", proj_1, proj_issue("PROJ-1", issuelinks=[]))
    h.server.add("GET", proj_1, proj_issue("PROJ-1", issuelinks=[outward]))
    h.server.add("GET", jira_path("/rest/api/2/issue/PROJ-2"), proj_issue("PROJ-2", issuelinks=[]))
    h.server.add("GET", jira_path("/rest/api/2/issueLinkType"), fixture("issue_link_types.json"))
    h.server.add("GET", PERMISSIONS, {"permissions": {"LINK_ISSUES": {"havePermission": True}}})
    h.server.add("POST", jira_path("/rest/api/2/issueLink"), status=201)


def _requirements(h: Harness) -> None:
    h.server.add("GET", PROJ_123, proj_issue(status={"name": "In Progress", "id": "3"}))
    h.server.add("GET", PROJ_123 + "/transitions", fixture("transitions_in_progress.json"))


def _transition(h: Harness) -> None:
    resolution = {
        "required": True,
        "schema": {"type": "resolution", "system": "resolution"},
        "name": "Resolution",
        "hasDefaultValue": False,
        "operations": ["set"],
        "allowedValues": [{"id": "1", "name": "Fixed"}, {"id": "2", "name": "Won't Fix"}],
    }
    done = {"id": "31", "name": "Done", "to": {"name": "Done", "id": "10001"}}
    transitions = {"transitions": [{**done, "fields": {"resolution": resolution}}]}
    h.server.add("GET", PROJ_123, proj_issue(status={"name": "In Progress", "id": "3"}))
    h.server.add("GET", PROJ_123 + "/transitions", transitions)
    h.server.add("POST", PROJ_123 + "/transitions", status=204)
    h.server.add(
        "GET",
        PROJ_123,
        proj_issue(status={"name": "Done", "id": "10001"}, resolution={"id": "1", "name": "Fixed"}),
    )


def _attachment(h: Harness) -> None:
    (h.tmp_path / "results.xml").write_text("<ok/>")

    def uploaded(request: httpx.Request) -> httpx.Response:
        request.read()
        return httpx.Response(200, json=[{"id": "500", "filename": "results.xml", "size": 5}])

    h.server.add("GET", PROJ_123, proj_issue())
    h.server.add(
        "GET", PERMISSIONS, {"permissions": {"CREATE_ATTACHMENTS": {"havePermission": True}}}
    )
    h.server.add(
        "GET", jira_path("/rest/api/2/attachment/meta"), {"enabled": True, "uploadLimit": 1000}
    )
    h.server.add("POST", PROJ_123 + "/attachments", handler=uploaded)


def _comment(h: Harness) -> None:
    h.server.add("GET", PROJ_123, proj_issue())
    h.server.add("GET", PERMISSIONS, {"permissions": {"ADD_COMMENTS": {"havePermission": True}}})
    h.server.add("POST", PROJ_123 + "/comment", {"id": "777"}, status=201)


def _jira_search(h: Harness) -> None:
    row = {
        "id": "1",
        "key": "PROJ-1",
        "fields": {"summary": "a", "status": {"name": "In Progress"}},
    }
    body = {"startAt": 0, "maxResults": 20, "total": 1, "issues": [row]}
    h.server.add("GET", jira_path("/rest/api/2/search"), body)


def _page_read(h: Harness) -> None:
    h.server.add("GET", PAGE, fixture("confluence_page.json"))


def _page_search(h: Harness) -> None:
    hit = {"id": "123456", "type": "page", "title": "Release notes", "space": {"key": "DOCS"}}
    body = {"results": [hit], "start": 0, "limit": 20, "size": 1}
    h.server.add("GET", confluence_path("/rest/api/content/search"), body)


def _page_create(h: Harness) -> None:
    doc = {"space_key": "DOCS", "title": "Release notes", "body_storage": "<p>Notes</p>"}
    (h.tmp_path / "page.json").write_text(json.dumps(doc))
    created = {
        "id": "123457",
        "type": "page",
        "title": "Release notes",
        "space": {"key": "DOCS"},
        "version": {"number": 1},
    }
    h.server.add("POST", confluence_path("/rest/api/content"), created)


def _page_update(h: Harness) -> None:
    doc = {"expected_version": 7, "body_storage": "<p>Updated</p>"}
    (h.tmp_path / "page.json").write_text(json.dumps(doc))
    page = fixture("confluence_page.json")
    h.server.add("GET", PAGE, page)
    h.server.add("PUT", PAGE, {**page, "version": {"number": 8}})


def _repo_list(h: Harness) -> None:
    h.server.add(
        "GET", bitbucket_path("/projects/PROJ/repos"), bb_page([fixture("bitbucket_repo.json")])
    )


def _repo_get(h: Harness) -> None:
    h.server.add("GET", BB_REPO, fixture("bitbucket_repo.json"))
    h.server.add(
        "GET", BB_REPO + "/branches/default", {"id": "refs/heads/main", "displayId": "main"}
    )


def _branch_list(h: Harness) -> None:
    h.server.add("GET", BB_REPO + "/branches", fixture("bitbucket_branches.json"))


def _pr_list(h: Harness) -> None:
    h.server.add(
        "GET", BB_REPO + "/pull-requests", bb_page([fixture("bitbucket_pull_request.json")])
    )


def _pr_get(h: Harness) -> None:
    h.server.add("GET", BB_PR, fixture("bitbucket_pull_request.json"))


def _pr_diff(h: Harness) -> None:
    one = fixture("bitbucket_diff.json")
    one["diffs"] = one["diffs"][:1]
    h.server.add("GET", BB_PR + "/diff/src/app.py", one)


def _pr_create(h: Harness) -> None:
    h.server.add("GET", BB_REPO, fixture("bitbucket_repo.json"))
    h.server.add("GET", BB_REPO + "/branches", fixture("bitbucket_branches.json"))
    h.server.add("GET", BB_REPO + "/pull-requests", bb_page([]))
    h.server.add(
        "POST", BB_REPO + "/pull-requests", fixture("bitbucket_pull_request.json"), status=201
    )


def _pr_comment(h: Harness) -> None:
    h.server.add("GET", BB_PR, fixture("bitbucket_pull_request.json"))
    h.server.add("GET", BB_PR + "/activities", fixture("bitbucket_activities.json"))
    h.server.add("POST", BB_PR + "/comments", fixture("bitbucket_comment.json"), status=201)
    h.server.add("GET", BB_PR + "/comments/777", fixture("bitbucket_comment.json"))


GOLDEN_SETUP: dict[str, Any] = {
    "commands": None,
    "schema": None,
    "login": _login,
    "config check": _config_check,
    "init": _init,
    "setup": None,
    "agent print": None,
    "agent install": None,
    "jira issue create": _issue_create,
    "jira issue get": _issue_get,
    "jira issue update": _issue_update,
    "jira issue link": _issue_link,
    "jira requirements": _requirements,
    "jira transition": _transition,
    "jira attachment add": _attachment,
    "jira comment add": _comment,
    "jira search": _jira_search,
    "confluence page read": _page_read,
    "confluence page search": _page_search,
    "confluence page create": _page_create,
    "confluence page update": _page_update,
    "bitbucket repo list": _repo_list,
    "bitbucket repo get": _repo_get,
    "bitbucket branch list": _branch_list,
    "bitbucket pr list": _pr_list,
    "bitbucket pr get": _pr_get,
    "bitbucket pr diff": _pr_diff,
    "bitbucket pr create": _pr_create,
    "bitbucket pr comment": _pr_comment,
}


def test_golden_tasks_cover_every_registered_command() -> None:
    assert set(GOLDEN_SETUP) == set(COMMANDS_BY_PATH)


@pytest.mark.parametrize("path", list(COMMANDS_BY_PATH))
def test_every_example_succeeds_in_one_call(harness: Harness, path: str) -> None:
    spec = COMMANDS_BY_PATH[path]
    argv = shlex.split(spec.example)
    if path == "setup":
        assert argv == ["yojo", "setup"]  # the registry shows the human launch path
    else:
        assert argv[:2] == ["yojo", "--json"]
    harness.write_config(GOLDEN_CONFIG)
    for service, token in (
        ("jira", JIRA_TOKEN),
        ("confluence", CONFLUENCE_TOKEN),
        ("bitbucket", BITBUCKET_TOKEN),
    ):
        harness.store(service, token)
    setup = GOLDEN_SETUP[path]
    if setup is not None:
        setup(harness)
    # A human runs login at a terminal; the one call gets only the hidden token.
    login = path == "login"
    result = harness.run(*argv[1:], tty=login, hidden=["jira-secret-token-456"] if login else None)
    if path in TERMINAL_ONLY:
        assert result.code == 2, result.out
        assert result.error["code"] == "usage_error"
        assert result.error["details"]["steps"]
        assert harness.server.requests == []
        return
    assert result.code == 0, result.out
    if path in OFFLINE:
        assert harness.server.requests == []
    if path in LOCAL_WRITES:
        assert (harness.tmp_path / "AGENTS.md").is_file()
    writes = harness.server.writes()
    assert len(writes) == (1 if path in REMOTE_WRITES else 0), [w.url.path for w in writes]
    if path in EXPECTED_WRITES:
        method, url_path, query, body = EXPECTED_WRITES[path]
        sent = writes[0]
        assert (sent.method, sent.url.path, dict(sent.url.params)) == (method, url_path, query)
        assert json.loads(sent.content) == body
    if path in REMOTE_WRITES:
        assert result.nbytes <= WRITE_BUDGET, (path, result.nbytes)
    elif path not in UNBUDGETED:
        assert result.nbytes <= READ_BUDGET, (path, result.nbytes)
    for secret in (
        JIRA_TOKEN,
        "jira-secret-token-456",
        CONFLUENCE_TOKEN,
        BITBUCKET_TOKEN,
    ):
        assert secret not in result.stdout


def test_setup_steps_run_non_interactively_from_an_empty_directory(harness: Harness) -> None:
    """The getting-started table, one command per step, saving to a non-default path."""
    assert list(harness.tmp_path.iterdir()) == []
    jira_url = "https://jira.example.test/jira"
    base = ["init", "--jira-url", jira_url]
    policy = harness.tmp_path / "policy.json"
    project = jira_path("/rest/api/2/project/PROJ")
    statuses = [{"name": "Bug", "statuses": [{"name": n} for n in ("To Do", "Done")]}]
    harness.server.add("GET", jira_path("/rest/api/2/myself"), {"name": "example-user"})
    harness.server.add("GET", project, fixture("jira_project.json"))
    harness.server.add("GET", FIELDS, fixture("fields.json"))
    harness.server.add("GET", project + "/statuses", statuses)
    harness.server.add(
        "GET",
        jira_path("/rest/api/2/issue/createmeta/PROJ/issuetypes"),
        fixture("createmeta_issuetypes.json"),
    )
    row = {"id": "1", "key": "PROJ-1", "fields": {"summary": "a", "status": {"name": "To Do"}}}
    search = {"startAt": 0, "maxResults": 5, "total": 1, "issues": [row]}
    harness.server.add("GET", jira_path("/rest/api/2/search"), search)

    # 1. Connect: without a login, init stops with the login command for a human.
    connect = harness.run(*base)
    assert connect.error["code"] == "auth_missing"
    assert f"yojo login --service jira --base-url {jira_url}" in connect.error["fix"]
    # 2. Log in (a person at a terminal; the token goes to the keyring only).
    login = harness.run(
        "login", "--service", "jira", "--base-url", jira_url, tty=True, hidden=[JIRA_TOKEN]
    )
    assert login.code == 0, login.out
    # 3. Choose projects.
    chosen = harness.run(*base, "--project", "PROJ")
    assert chosen.data["projects"] == [{"key": "PROJ", "name": "Example Project"}]
    # 4. Map fields and rules, checked against live Jira.
    policy.write_text(json.dumps({"jira": {"fields": {"points": {"jira_name": "Story Points"}}}}))
    mapped = [*base, "--project", "PROJ", "--policy-input", "policy.json"]
    preview = harness.run(*mapped, "--output", "config/team.yaml")
    assert preview.data["action"] == "preview"
    # 5. Review and save, exactly as the preview says.
    (save_step,) = preview.data["next"]
    saved = harness.run(*shlex.split(save_step)[1:])
    assert saved.data["action"] == "created", saved.out
    config = harness.tmp_path / "config" / "team.yaml"
    assert config.read_text() == preview.data["yaml"]
    assert not (harness.tmp_path / "yojo.yaml").exists()
    # 6 and 7. Check, connect the agent, and try one read, from the same directory.
    check, agent, read = saved.data["next"]
    assert "agent install" in agent
    for step in (check, agent, read):
        ran = harness.run(*shlex.split(step)[1:])
        assert ran.code == 0, (step, ran.out)
    assert (harness.tmp_path / "AGENTS.md").is_file()
    assert harness.server.paths("GET")[-1] == jira_path("/rest/api/2/search")
    assert harness.server.writes() == []
    assert JIRA_TOKEN not in config.read_text()
