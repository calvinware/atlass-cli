"""Bitbucket Data Center: login, reads, cursors, diff limits, and checked writes.

Every response is a fixture modeled on the Data Center REST 1.0 reference; no test reaches a
real server or keyring. A refused write must send zero POSTs; an uncertain write must offer an
inspection, never a retry.
"""

from __future__ import annotations

import copy
import json
import re
import shlex
from typing import Any

import httpx
import pytest

from tests.conftest import (
    BITBUCKET,
    BITBUCKET_TOKEN,
    Harness,
    bitbucket_path,
    fixture,
    run_argv,
)
from yojo import transport
from yojo.contracts import COMMANDS

WRITE_BUDGET = 2 * 1024
READ_BUDGET = 4 * 1024

PROPS = bitbucket_path("/application-properties")
RECENT = bitbucket_path("/profile/recent/repos")
REPOS = bitbucket_path("/repos")
PROJECT_REPOS = bitbucket_path("/projects/PROJ/repos")
REPO = bitbucket_path("/projects/PROJ/repos/web")
DEFAULT_BRANCH = REPO + "/default-branch"
BRANCHES = REPO + "/branches"
PRS = REPO + "/pull-requests"
PR = PRS + "/42"
DIFF = PR + "/diff"
ACTIVITIES = PR + "/activities"
COMMENTS = PR + "/comments"

CREATE_ARGV = [
    "bitbucket",
    "pr",
    "create",
    "--repo",
    "PROJ/web",
    "--source",
    "feature/login",
    "--target",
    "main",
    "--title",
    "Fix login on Safari",
]
COMMENT_ARGV = ["bitbucket", "pr", "comment", "42", "--repo", "PROJ/web"]


def config(harness: Harness) -> None:
    harness.write_config(f"version: 1\nbitbucket:\n  base_url: {BITBUCKET}\n")
    harness.store("bitbucket", BITBUCKET_TOKEN)


def page(values: list[Any], *, start: int = 0, next_start: int | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "size": len(values),
        "limit": 25,
        "start": start,
        "isLastPage": next_start is None,
        "values": values,
    }
    if next_start is not None:
        body["nextPageStart"] = next_start
    return body


def branches_handler(request: httpx.Request) -> httpx.Response:
    """Bitbucket's filterText is a case-insensitive substring match on the branch name."""
    wanted = request.url.params.get("filterText", "").casefold()
    values = [b for b in fixture("bitbucket_branches.json")["values"] if wanted in b["displayId"]]
    return httpx.Response(200, json=page(values))


def created_pr(request: httpx.Request) -> httpx.Response:
    sent = json.loads(request.content)
    pr = fixture("bitbucket_pull_request.json")
    pr["title"] = sent["title"]
    pr["description"] = sent.get("description")
    return httpx.Response(201, json=pr)


def setup_create(harness: Harness, open_prs: list[Any] | None = None) -> None:
    config(harness)
    harness.server.add("GET", REPO, fixture("bitbucket_repo.json"))
    harness.server.add("GET", BRANCHES, handler=branches_handler)
    harness.server.add("GET", PRS, page(open_prs or []))
    harness.server.add("POST", PRS, handler=created_pr)


def setup_comment(harness: Harness) -> None:
    config(harness)
    harness.server.add("GET", PR, fixture("bitbucket_pull_request.json"))
    harness.server.add("GET", ACTIVITIES, fixture("bitbucket_activities.json"))
    harness.server.add("POST", COMMENTS, fixture("bitbucket_comment.json"), status=201)

    def scoped_comment(_: httpx.Request) -> httpx.Response:
        sent = next(
            r
            for r in reversed(harness.server.requests)
            if r.method == "POST" and r.url.path == COMMENTS
        )
        result = fixture("bitbucket_comment.json")
        result["text"] = json.loads(sent.content)["text"]
        return httpx.Response(200, json=result)

    harness.server.add("GET", COMMENTS + "/777", handler=scoped_comment)


def refusal(result: Any) -> dict[str, Any]:
    assert result.code == 4, result.out
    error = result.error
    assert error["code"] == "requirements_unmet"
    details: dict[str, Any] = error["details"]
    assert details["nothing_sent"] is True
    if details["retry_argv"] is not None:
        assert details["retry"] == shlex.join(details["retry_argv"])
        assert run_argv(details["retry_argv"]) == details["retry_argv"]
    return details


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


# --------------------------------------------------------------------------- login


def test_login_checks_application_and_token_before_storing(harness: Harness) -> None:
    harness.server.add("GET", PROPS, fixture("bitbucket_application_properties.json"))
    harness.server.add("GET", RECENT, page([]))
    result = harness.run(
        "login",
        "--service",
        "bitbucket",
        "--base-url",
        BITBUCKET + "/",
        "--token-stdin",
        stdin=BITBUCKET_TOKEN.encode() + b"\n",
    )
    assert result.code == 0, result.out
    (outcome,) = result.data["services"]
    assert outcome == {
        "service": "bitbucket",
        "auth": "pat",
        "base_url": BITBUCKET,
        "user": None,
        "version": "8.19.1",
        "status": "stored",
    }
    assert harness.server.paths() == [PROPS, RECENT]
    assert all(
        r.headers["authorization"] == f"Bearer {BITBUCKET_TOKEN}" for r in harness.server.requests
    )
    assert json.loads(harness.stored("bitbucket") or "{}")["secret"] == BITBUCKET_TOKEN
    assert BITBUCKET_TOKEN not in result.stdout + result.stderr


def test_login_refuses_a_server_that_is_not_bitbucket(harness: Harness) -> None:
    harness.server.add("GET", PROPS, {"version": "9.12.0", "displayName": "JIRA"})
    result = harness.run(
        "login",
        "--service",
        "bitbucket",
        "--base-url",
        BITBUCKET,
        "--token-stdin",
        stdin=b"tok\n",
    )
    assert result.code == 6
    assert result.error["code"] == "response_invalid"
    assert harness.stored("bitbucket") is None
    assert harness.server.paths() == [PROPS]


def test_login_needs_the_authenticated_endpoint_to_accept_the_token(harness: Harness) -> None:
    # application-properties answers anonymously, so on its own it proves nothing about the token.
    harness.server.add("GET", PROPS, fixture("bitbucket_application_properties.json"))
    harness.server.add(
        "GET",
        RECENT,
        {"errors": [{"context": None, "message": "Authentication failed"}]},
        status=401,
    )
    result = harness.run(
        "login",
        "--service",
        "bitbucket",
        "--base-url",
        BITBUCKET,
        "--token-stdin",
        stdin=b"wrong-token\n",
    )
    assert result.code == 3
    assert result.error["code"] == "auth_failed"
    assert result.error["details"]["messages"] == ["Authentication failed"]
    assert harness.stored("bitbucket") is None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"errors": [{"context": None, "message": "Authentication failed"}]},
        {"size": 0, "isLastPage": True, "start": 0, "values": "nope"},
        {"size": 0, "isLastPage": True, "start": 0, "values": {}},
        {"size": 0, "isLastPage": True, "start": 0, "values": None},
    ],
    ids=["empty", "errors", "values-string", "values-object", "values-null"],
)
def test_login_refuses_a_200_that_does_not_confirm_the_user(
    harness: Harness, body: dict[str, Any]
) -> None:
    harness.server.add("GET", PROPS, fixture("bitbucket_application_properties.json"))
    harness.server.add("GET", RECENT, body)
    result = harness.run(
        "login",
        "--service",
        "bitbucket",
        "--base-url",
        BITBUCKET,
        "--token-stdin",
        stdin=b"tok\n",
    )
    assert result.code == 3
    assert result.error["code"] == "auth_failed"
    assert harness.stored("bitbucket") is None
    assert harness.server.paths() == [PROPS, RECENT]


@pytest.mark.parametrize("extra", [["--auth", "basic", "--username", "u"], ["--password-stdin"]])
def test_login_refuses_passwords_for_bitbucket(harness: Harness, extra: list[str]) -> None:
    result = harness.run("login", "--service", "bitbucket", "--base-url", BITBUCKET, *extra)
    assert result.code == 2
    assert "access token" in result.error["message"]
    assert harness.server.requests == []


def test_login_all_still_means_jira_and_confluence(harness: Harness) -> None:
    harness.write_config(f"version: 1\nbitbucket:\n  base_url: {BITBUCKET}\n")
    result = harness.run("login", "--service", "all", "--token-stdin", stdin=b"a\nb\n")
    assert result.code == 2
    assert result.error["code"] == "service_not_configured"
    assert result.error["details"]["service"] == "jira"


# --------------------------------------------------------------------------- config check


def test_config_check_bitbucket_only(harness: Harness) -> None:
    config(harness)
    harness.server.add("GET", PROPS, fixture("bitbucket_application_properties.json"))
    harness.server.add("GET", RECENT, page([]))
    result = harness.run("config", "check")
    assert result.code == 0, result.out
    assert [c["id"] for c in result.data["checks"]] == ["bitbucket:application", "bitbucket:auth"]
    assert result.data["summary"] == {"pass": 2}
    assert "8.19.1" in result.data["checks"][0]["message"]


def test_config_check_reports_a_missing_bitbucket_login(harness: Harness) -> None:
    harness.write_config(f"version: 1\nbitbucket:\n  base_url: {BITBUCKET}\n")
    result = harness.run("config", "check")
    assert result.code == 4
    assert result.error["code"] == "config_unverified"
    assert "live Bitbucket" in result.error["message"]
    (check,) = result.error["details"]["checks"]
    assert check["id"] == "bitbucket:auth"
    assert "yojo login --service bitbucket" in check["fix"]
    assert harness.server.requests == []


def test_config_check_flags_a_url_that_is_not_bitbucket(harness: Harness) -> None:
    config(harness)
    harness.server.add("GET", PROPS, status=404)
    result = harness.run("config", "check")
    assert result.code == 4
    assert result.error["code"] == "config_unverified"
    harness.server.routes[("GET", PROPS)] = []
    harness.server.add("GET", PROPS, {"displayName": "Confluence", "version": "8.5.0"})
    result = harness.run("config", "check")
    assert result.error["code"] == "config_drift"
    assert "bitbucket.base_url" in result.error["details"]["checks"][0]["fix"]


def test_explicit_url_must_match_configured_bitbucket(harness: Harness) -> None:
    config(harness)
    result = harness.run(
        "bitbucket", "repo", "get", "PROJ/web", "--base-url", "https://other.example.test"
    )
    assert result.code == 2
    assert result.error["code"] == "input_invalid"
    assert harness.server.requests == []


def test_works_without_config_using_base_url(harness: Harness) -> None:
    harness.store("bitbucket", BITBUCKET_TOKEN)
    harness.server.add("GET", REPO, fixture("bitbucket_repo.json"))
    harness.server.add("GET", DEFAULT_BRANCH, fixture("bitbucket_branches.json")["values"][0])
    result = harness.run("bitbucket", "repo", "get", "PROJ/web", "--base-url", BITBUCKET + "/")
    assert result.code == 0, result.out
    assert result.data["default_branch"] == "main"


# --------------------------------------------------------------------------- reads


def test_repo_list_uses_the_server_cursor(harness: Harness) -> None:
    config(harness)
    repo = fixture("bitbucket_repo.json")
    other = {**copy.deepcopy(repo), "slug": "api", "name": "API service"}
    # Bitbucket may skip filtered items, so the cursor is not start + limit.
    harness.server.add("GET", PROJECT_REPOS, page([repo, other], start=5, next_start=11))
    result = harness.run(
        "bitbucket", "repo", "list", "--project", "PROJ", "--start", "5", "--limit", "2"
    )
    assert result.code == 0, result.out
    assert result.data["next_start"] == 11
    assert [r["repo"] for r in result.data["repos"]] == ["PROJ/web", "PROJ/api"]
    assert result.data["repos"][0]["url"] == f"{BITBUCKET}/projects/PROJ/repos/web/browse"
    params = harness.server.requests[0].url.params
    assert (params["start"], params["limit"]) == ("5", "2")


def test_global_repo_list_and_last_page(harness: Harness) -> None:
    config(harness)
    harness.server.add("GET", REPOS, page([fixture("bitbucket_repo.json")]))
    result = harness.run("bitbucket", "repo", "list")
    assert result.data["next_start"] is None
    assert result.data["project"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"size": 0, "isLastPage": False, "start": 0, "values": []},
        {"size": 0, "isLastPage": False, "start": 0, "nextPageStart": 0, "values": []},
        {"size": 0, "isLastPage": False, "start": 0, "nextPageStart": "20", "values": []},
        {"size": 0, "start": 0, "values": []},
        {"size": 0, "isLastPage": True, "start": 40, "values": []},
        {"size": 0, "isLastPage": True, "values": []},
        {"size": 0, "isLastPage": True, "start": "0", "values": []},
        {"size": 21, "isLastPage": True, "start": 0, "values": [{}] * 21},
        {"isLastPage": True, "start": 0, "values": "nope"},
        {"isLastPage": True, "start": 0, "values": []},
        {"size": 2, "isLastPage": True, "start": 0, "values": [{}]},
        {"size": -1, "isLastPage": True, "start": 0, "values": []},
        {"size": "0", "isLastPage": True, "start": 0, "values": []},
        {"size": True, "isLastPage": True, "start": 0, "values": [{}]},
    ],
    ids=[
        "missing",
        "repeating",
        "not-int",
        "no-last-flag",
        "wrong-start",
        "no-start",
        "start-not-int",
        "too-many",
        "values",
        "no-size",
        "size-mismatch",
        "size-negative",
        "size-not-int",
        "size-bool",
    ],
)
def test_malformed_pagination_fails_closed(harness: Harness, body: dict[str, Any]) -> None:
    config(harness)
    harness.server.add("GET", REPOS, body)
    result = harness.run("bitbucket", "repo", "list")
    assert result.code == 6
    assert result.error["code"] == "response_invalid"


def test_backward_cursor_is_refused(harness: Harness) -> None:
    config(harness)
    harness.server.add("GET", BRANCHES, page([], start=50, next_start=10))
    result = harness.run("bitbucket", "branch", "list", "--repo", "PROJ/web", "--start", "50")
    assert result.error["code"] == "response_invalid"
    assert "next cursor" in result.error["message"]


def test_repo_get(harness: Harness) -> None:
    config(harness)
    harness.server.add("GET", REPO, fixture("bitbucket_repo.json"))
    harness.server.add("GET", DEFAULT_BRANCH, status=404)
    data = harness.run("bitbucket", "repo", "get", "PROJ/web").data
    assert data["repo"] == "PROJ/web"
    assert data["default_branch"] is None
    assert data["clone_ssh"] == "ssh://git@bitbucket.example.test:7999/proj/web.git"
    assert data["content_trust"] == "untrusted"


def test_repo_get_keeps_the_configured_default_of_an_empty_repository(harness: Harness) -> None:
    # An empty repository has no branches yet, but its configured default ref still answers.
    config(harness)
    harness.server.add("GET", REPO, fixture("bitbucket_repo.json"))
    harness.server.add("GET", BRANCHES, page([]))
    harness.server.add(
        "GET", DEFAULT_BRANCH, {"id": "refs/heads/main", "displayId": "main", "type": "BRANCH"}
    )
    result = harness.run("bitbucket", "repo", "get", "PROJ/web")
    assert result.code == 0, result.out
    assert result.data["default_branch"] == "main"
    assert harness.server.paths() == [REPO, DEFAULT_BRANCH]


def test_repo_get_with_no_default_branch_body(harness: Harness) -> None:
    config(harness)
    harness.server.add("GET", REPO, fixture("bitbucket_repo.json"))
    harness.server.add("GET", DEFAULT_BRANCH, status=204)
    result = harness.run("bitbucket", "repo", "get", "PROJ/web")
    assert result.code == 0, result.out
    assert result.data["default_branch"] is None


@pytest.mark.parametrize(
    "branch", [{}, {"displayId": "main"}, {"displayId": "main", "id": "refs/heads/other"}]
)
def test_repo_get_refuses_malformed_default_branch(
    harness: Harness, branch: dict[str, Any]
) -> None:
    config(harness)
    harness.server.add("GET", REPO, fixture("bitbucket_repo.json"))
    harness.server.add("GET", DEFAULT_BRANCH, branch)
    result = harness.run("bitbucket", "repo", "get", "PROJ/web")
    assert result.error["code"] == "response_invalid"


def test_repo_get_rejects_a_different_repository(harness: Harness) -> None:
    config(harness)
    harness.server.add("GET", REPO, {**fixture("bitbucket_repo.json"), "slug": "other"})
    assert harness.run("bitbucket", "repo", "get", "PROJ/web").error["code"] == "response_invalid"


@pytest.mark.parametrize("value", ["PROJ", "PROJ/", "/web", "PROJ/web/extra", "PROJ/we b"])
def test_repo_must_be_project_slash_slug(harness: Harness, value: str) -> None:
    result = harness.run("bitbucket", "repo", "get", value)
    assert result.code == 2
    assert result.error["code"] == "usage_error"


def test_branch_list_filters(harness: Harness) -> None:
    config(harness)
    harness.server.add("GET", BRANCHES, handler=branches_handler)
    result = harness.run("bitbucket", "branch", "list", "--repo", "PROJ/web", "--filter", "login")
    assert [b["name"] for b in result.data["branches"]] == ["feature/login", "feature/login-v2"]
    assert harness.server.requests[0].url.params["filterText"] == "login"
    assert result.data["branches"][0]["commit"] == "0a943a29376f2336b78312d99e65da17048951db"


def test_pr_list_state_any_case_and_compact_titles(harness: Harness) -> None:
    config(harness)
    pr = fixture("bitbucket_pull_request.json")
    pr["title"] = "t" * 300
    harness.server.add("GET", PRS, page([pr] * 20, next_start=20))
    result = harness.run("bitbucket", "pr", "list", "--repo", "PROJ/web", "--state", "merged")
    assert result.code == 0, result.out
    assert harness.server.requests[0].url.params["state"] == "MERGED"
    row = result.data["pull_requests"][0]
    assert row == {
        "id": 42,
        "title": "t" * 57 + "...",
        "state": "OPEN",
        "source": "feature/login",
        "target": "main",
        "author": "Example User",
        "updated": "2026-10-28T12:53:31Z",
    }
    assert result.data["next_start"] == 20
    assert result.nbytes <= READ_BUDGET


def test_pr_list_empty_state_in_terminal(harness: Harness) -> None:
    config(harness)
    harness.server.add("GET", PRS, page([]))
    tty = harness.run_tty("bitbucket", "pr", "list", "--repo", "PROJ/web")
    assert tty.code == 0
    assert "No open pull requests in PROJ/web." in tty.stdout


def test_pr_get_trims_description_unless_full(harness: Harness) -> None:
    config(harness)
    pr = fixture("bitbucket_pull_request.json")
    pr["description"] = "d" * 10_000
    harness.server.add("GET", PR, pr)
    result = harness.run("bitbucket", "pr", "get", "42", "--repo", "PROJ/web")
    data = result.data
    assert data["truncated"] is True
    assert data["more"] == "yojo bitbucket pr get 42 --repo PROJ/web --full"
    assert data["reviewers"] == [{"name": "Example Reviewer", "status": "UNAPPROVED"}]
    assert data["url"] == f"{BITBUCKET}/projects/PROJ/repos/web/pull-requests/42"
    assert result.nbytes <= READ_BUDGET
    full = harness.run("bitbucket", "pr", "get", "42", "--repo", "PROJ/web", "--full")
    assert full.data["description"] == "d" * 10_000


def _drop(key: str) -> Any:
    def edit(pr: dict[str, Any]) -> None:
        del pr[key]

    return edit


def _put(key: str, value: Any) -> Any:
    def edit(pr: dict[str, Any]) -> None:
        pr[key] = value

    return edit


MALFORMED_PR = [
    _drop("state"),
    _put("state", None),
    _put("state", "open"),
    _put("state", "SUPERSEDED"),
    _drop("fromRef"),
    _put("fromRef", "refs/heads/feature/login"),
    _put("fromRef", {"displayId": "feature/login"}),
    _put("fromRef", {"id": ""}),
    _drop("toRef"),
    _put("toRef", {"id": 7}),
]
MALFORMED_PR_IDS = [
    "no-state",
    "null-state",
    "lowercase-state",
    "unknown-state",
    "no-fromRef",
    "fromRef-string",
    "fromRef-no-id",
    "fromRef-empty-id",
    "no-toRef",
    "toRef-id-not-string",
]


@pytest.mark.parametrize("edit", MALFORMED_PR, ids=MALFORMED_PR_IDS)
def test_pr_list_refuses_malformed_pull_requests(harness: Harness, edit: Any) -> None:
    config(harness)
    pr = fixture("bitbucket_pull_request.json")
    edit(pr)
    harness.server.add("GET", PRS, page([pr]))
    result = harness.run("bitbucket", "pr", "list", "--repo", "PROJ/web")
    assert result.code == 6
    assert result.error["code"] == "response_invalid"


@pytest.mark.parametrize("edit", MALFORMED_PR, ids=MALFORMED_PR_IDS)
def test_pr_get_refuses_malformed_pull_requests(harness: Harness, edit: Any) -> None:
    config(harness)
    pr = fixture("bitbucket_pull_request.json")
    edit(pr)
    harness.server.add("GET", PR, pr)
    result = harness.run("bitbucket", "pr", "get", "42", "--repo", "PROJ/web")
    assert result.code == 6
    assert result.error["code"] == "response_invalid"


def test_pr_get_rejects_another_pull_request(harness: Harness) -> None:
    config(harness)
    harness.server.add("GET", PR, {**fixture("bitbucket_pull_request.json"), "id": 43})
    assert harness.run("bitbucket", "pr", "get", "42", "--repo", "PROJ/web").code == 6


# --------------------------------------------------------------------------- diff


def _diff_path_body(path: str) -> dict[str, Any]:
    return {"components": path.split("/"), "toString": path}


def test_pr_diff_is_unified_and_complete_when_small(harness: Harness) -> None:
    config(harness)
    diff = fixture("bitbucket_diff.json")
    diff["diffs"] += [
        {
            "source": _diff_path_body("docs/old.md"),
            "destination": None,
            "hunks": [
                {
                    "sourceLine": 1,
                    "sourceSpan": 1,
                    "destinationLine": 0,
                    "destinationSpan": 0,
                    "segments": [{"type": "REMOVED", "lines": [{"line": "# Old"}]}],
                }
            ],
        },
        {
            "source": _diff_path_body("img/logo.png"),
            "destination": _diff_path_body("img/logo.png"),
            "binary": True,
        },
    ]
    harness.server.add("GET", DIFF, diff)
    result = harness.run("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web")
    data = result.data
    assert data["complete"] is True and data["more"] is None and data["inspect"] is None
    assert data["files_total"] == 4
    assert data["files"] == [
        {"path": "src/app.py", "old_path": None, "change": "modified", "added": 2, "removed": 1},
        {"path": "docs/login.md", "old_path": None, "change": "added", "added": 1, "removed": 0},
        {"path": "docs/old.md", "old_path": None, "change": "deleted", "added": 0, "removed": 1},
        {"path": "img/logo.png", "old_path": None, "change": "modified", "added": 0, "removed": 0},
    ]
    assert data["diff"] == (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -10,2 +10,3 @@\n"
        " def login(user):\n"
        "-    return check(user)\n"
        "+    if not user:\n"
        "+        return False\n"
        "diff --git a/docs/login.md b/docs/login.md\n"
        "--- /dev/null\n"
        "+++ b/docs/login.md\n"
        "@@ -0,0 +1,1 @@\n"
        "+# Login\n"
        "diff --git a/docs/old.md b/docs/old.md\n"
        "--- a/docs/old.md\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        "-# Old\n"
        "diff --git a/img/logo.png b/img/logo.png\n"
        "--- a/img/logo.png\n"
        "+++ b/img/logo.png\n"
        "Binary files differ\n"
    )
    assert data["diff_chars"] == len(data["diff"])
    assert harness.server.requests[0].url.params["withComments"] == "false"


@pytest.mark.parametrize("side", ["sourceSpan", "destinationSpan"])
def test_pr_diff_with_hunk_spans_that_do_not_add_up_is_incomplete(
    harness: Harness, side: str
) -> None:
    config(harness)
    diff = fixture("bitbucket_diff.json")
    diff["diffs"][0]["hunks"][0][side] += 5
    harness.server.add("GET", DIFF, diff)
    data = harness.run("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web", "--full").data
    assert data["complete"] is False and data["server_truncated"] is True
    assert data["truncated_files"] == ["src/app.py"]
    assert data["inspect"] == f"git diff {diff['fromHash']} {diff['toHash']} -- src/app.py"


def test_pr_diff_marks_local_truncation(harness: Harness) -> None:
    config(harness)
    diff = fixture("bitbucket_diff.json")
    lines = diff["diffs"][0]["hunks"][0]["segments"][2]["lines"]
    lines += [{"line": f"    step_{n} = {n}", "truncated": False} for n in range(400)]
    diff["diffs"][0]["hunks"][0]["destinationSpan"] += 400
    harness.server.add("GET", DIFF, diff)
    data = harness.run("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web").data
    assert data["truncated"] is True and data["complete"] is False
    assert data["diff"].endswith("\n")
    assert len(data["diff"]) < data["diff_chars"]
    assert data["more"] == "yojo bitbucket pr diff 42 --repo PROJ/web --full"
    full = harness.run("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web", "--full").data
    assert full["complete"] is True
    assert len(full["diff"]) == full["diff_chars"]


def _rename(diff: dict[str, Any], old: str, new: str) -> None:
    """Rename the second fixture file (an added file) to a move from old to new."""
    entry = diff["diffs"][1]
    entry["source"] = {"components": old.split("/"), "toString": old}
    entry["destination"] = {"components": new.split("/"), "toString": new}


def test_pr_diff_marks_server_truncation_and_points_at_the_file(harness: Harness) -> None:
    config(harness)
    diff = fixture("bitbucket_diff.json")
    diff["diffs"][1]["hunks"][0]["truncated"] = True
    diff["truncated"] = True
    harness.server.add("GET", DIFF, diff)
    data = harness.run("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web", "--full").data
    assert data["server_truncated"] is True and data["complete"] is False
    assert data["truncated_files"] == ["docs/login.md"]
    assert data["more"] == "yojo bitbucket pr diff 42 --repo PROJ/web --path docs/login.md --full"
    assert data["inspect"] == f"git diff {diff['fromHash']} {diff['toHash']}"
    tty = harness.run_tty("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web", "--full")
    assert "Incomplete: Bitbucket cut part of the diff." in tty.stdout
    assert f"In a clone with both commits: git diff {diff['fromHash']}" in tty.stdout


@pytest.mark.parametrize("level", ["diff", "file", "hunk", "segment", "line"])
def test_pr_diff_reads_truncated_as_a_string(harness: Harness, level: str) -> None:
    config(harness)
    diff = fixture("bitbucket_diff.json")
    entry = diff["diffs"][0]
    hunk = entry["hunks"][0]
    target = {
        "diff": diff,
        "file": entry,
        "hunk": hunk,
        "segment": hunk["segments"][0],
        "line": hunk["segments"][0]["lines"][0],
    }[level]
    target["truncated"] = "true"
    diff["diffs"][1]["truncated"] = "false"
    harness.server.add("GET", DIFF, diff)
    data = harness.run("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web", "--full").data
    assert data["server_truncated"] is True and data["complete"] is False
    assert data["truncated_files"] == ([] if level == "diff" else ["src/app.py"])


def test_pr_diff_rejects_an_unknown_truncated_value(harness: Harness) -> None:
    config(harness)
    diff = fixture("bitbucket_diff.json")
    diff["diffs"][0]["truncated"] = "partly"
    harness.server.add("GET", DIFF, diff)
    result = harness.run("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web")
    assert result.error["code"] == "response_invalid"


def test_pr_diff_file_that_bitbucket_returns_in_full(harness: Harness) -> None:
    config(harness)
    whole = fixture("bitbucket_diff.json")
    whole["diffs"][1]["truncated"] = True
    harness.server.add("GET", DIFF, whole)
    data = harness.run("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web", "--full").data
    assert data["complete"] is False
    one = fixture("bitbucket_diff.json")
    one["diffs"] = one["diffs"][1:]
    harness.server.add("GET", DIFF + "/docs/login.md", one)
    data = harness.run(*shlex.split(data["more"])[1:]).data
    assert data["complete"] is True and data["more"] is None and data["inspect"] is None


def test_pr_diff_file_still_cut_names_a_git_command(harness: Harness) -> None:
    config(harness)
    one = fixture("bitbucket_diff.json")
    one["diffs"] = one["diffs"][1:]
    one["diffs"][0]["truncated"] = True
    harness.server.add("GET", DIFF + "/docs/login.md", one)
    argv = ["bitbucket", "pr", "diff", "42", "--repo", "PROJ/web", "--path", "docs/login.md"]
    data = harness.run(*argv, "--full").data
    assert data["complete"] is False and data["more"] is None
    assert data["truncated_files"] == ["docs/login.md"]
    assert data["inspect"] == f"git diff {one['fromHash']} {one['toHash']} -- docs/login.md"


def test_pr_diff_renamed_file_passes_the_old_path(harness: Harness) -> None:
    config(harness)
    diff = fixture("bitbucket_diff.json")
    _rename(diff, "docs/signin.md", "docs/login.md")
    diff["diffs"][1]["truncated"] = True
    harness.server.add("GET", DIFF, diff)
    data = harness.run("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web", "--full").data
    assert data["files"][1] == {
        "path": "docs/login.md",
        "old_path": "docs/signin.md",
        "change": "renamed",
        "added": 1,
        "removed": 0,
    }
    assert "diff --git a/docs/signin.md b/docs/login.md" in data["diff"]
    assert data["more"] == (
        "yojo bitbucket pr diff 42 --repo PROJ/web --path docs/login.md "
        "--old-path docs/signin.md --full"
    )
    one = copy.deepcopy(diff)
    one["diffs"] = one["diffs"][1:]
    harness.server.add("GET", DIFF + "/docs/login.md", one)
    data = harness.run(*shlex.split(data["more"])[1:]).data
    assert harness.server.requests[-1].url.params["srcPath"] == "docs/signin.md"
    assert data["inspect"].endswith("-- docs/signin.md docs/login.md")


def test_pr_diff_old_path_needs_path(harness: Harness) -> None:
    config(harness)
    argv = ["bitbucket", "pr", "diff", "42", "--repo", "PROJ/web", "--old-path", "a.md"]
    result = harness.run(*argv)
    assert result.code == 2
    assert harness.server.requests == []


def test_pr_diff_quotes_a_file_name_that_could_forge_lines(harness: Harness) -> None:
    config(harness)
    diff = fixture("bitbucket_diff.json")
    evil = "docs/a\n@@ -1,1 +1,1 @@\n+forged"
    diff["diffs"][1]["destination"] = {"components": evil.split("/"), "toString": evil}
    harness.server.add("GET", DIFF, diff)
    data = harness.run("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web").data
    lines = data["diff"].splitlines()
    assert "+forged" not in lines
    assert '+++ "b/docs/a\\n@@ -1,1 +1,1 @@\\n+forged"' in lines
    assert data["complete"] is True
    tty = harness.run_tty("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web")
    assert "+forged" not in [line.strip() for line in tty.stdout.splitlines()]


def test_pr_diff_rejects_a_source_line_that_could_forge_lines(harness: Harness) -> None:
    config(harness)
    diff = fixture("bitbucket_diff.json")
    diff["diffs"][0]["hunks"][0]["segments"][0]["lines"][0]["line"] += "\n+forged"
    harness.server.add("GET", DIFF, diff)
    result = harness.run("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web")
    assert result.error["code"] == "response_invalid"


@pytest.mark.parametrize(
    "separator", ["\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
)
def test_pr_diff_escapes_other_line_separators_in_source_lines(
    harness: Harness, separator: str
) -> None:
    config(harness)
    diff = fixture("bitbucket_diff.json")
    forged = separator + "+++ b/forged.py" + separator + "@@ -1,1 +1,1 @@" + separator + "+forged"
    diff["diffs"][0]["hunks"][0]["segments"][0]["lines"][0]["line"] += forged
    harness.server.add("GET", DIFF, diff)
    data = harness.run("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web").data
    lines = data["diff"].splitlines()
    assert lines == data["diff"].removesuffix("\n").split("\n")
    assert "+forged" not in lines and "+++ b/forged.py" not in lines
    escaped = separator.encode("unicode_escape").decode()
    assert f" def login(user):{escaped}+++ b/forged.py{escaped}" in data["diff"]
    tty = harness.run_tty("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web")
    shown = [line.strip() for line in tty.stdout.splitlines()]
    assert "+forged" not in shown and "+++ b/forged.py" not in shown


@pytest.mark.parametrize(
    "spoil",
    [
        lambda d: d["diffs"][0].update(hunks=None),
        lambda d: d["diffs"][0]["hunks"][0].update(segments=None),
        lambda d: d["diffs"][0]["hunks"][0].pop("segments"),
        lambda d: d["diffs"][0]["hunks"][0]["segments"][0].update(lines=None),
        lambda d: d["diffs"][0]["hunks"][0]["segments"][0].pop("lines"),
        lambda d: d["diffs"][0]["hunks"].append(None),
        lambda d: d["diffs"][0]["hunks"][0]["segments"].append(None),
    ],
)
def test_pr_diff_rejects_malformed_hunks_instead_of_an_empty_complete_diff(
    harness: Harness, spoil: Any
) -> None:
    config(harness)
    diff = fixture("bitbucket_diff.json")
    spoil(diff)
    harness.server.add("GET", DIFF, diff)
    result = harness.run("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web")
    assert result.code == 6
    assert result.error["code"] == "response_invalid"


def test_pr_diff_git_inspection_uses_full_scope_for_an_unsafe_path(harness: Harness) -> None:
    config(harness)
    diff = fixture("bitbucket_diff.json")
    path = "docs/a\nforged"
    diff["diffs"][1]["destination"] = {"toString": path}
    diff["diffs"][1]["truncated"] = True
    harness.server.add("GET", DIFF, diff)
    data = harness.run("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web", "--full").data
    assert data["inspect"] == f"git diff {diff['fromHash']} {diff['toHash']}"
    assert data["more"] is None


def test_pr_diff_for_one_file(harness: Harness) -> None:
    config(harness)
    diff = fixture("bitbucket_diff.json")
    diff["diffs"] = diff["diffs"][1:]
    diff["diffs"][0]["destination"] = {
        "components": ["docs", "login notes.md"],
        "toString": "docs/login notes.md",
    }
    harness.server.add("GET", DIFF + "/docs/login notes.md", diff)
    result = harness.run(
        "bitbucket", "pr", "diff", "42", "--repo", "PROJ/web", "--path", "docs/login notes.md"
    )
    assert result.code == 0, result.out
    assert result.data["path"] == "docs/login notes.md"
    assert [f["path"] for f in result.data["files"]] == ["docs/login notes.md"]
    assert result.data["complete"] is True
    assert harness.server.requests[0].url.raw_path.startswith(
        b"/bitbucket/rest/api/1.0/projects/PROJ/repos/web/pull-requests/42/diff/docs/login%20notes.md"
    )
    assert result.data["url"].endswith("/pull-requests/42/diff#docs/login%20notes.md")


def test_pr_diff_for_one_file_rejects_another_file(harness: Harness) -> None:
    config(harness)
    diff = fixture("bitbucket_diff.json")
    diff["diffs"] = diff["diffs"][1:]
    harness.server.add("GET", DIFF + "/src/other.py", diff)
    argv = ["bitbucket", "pr", "diff", "42", "--repo", "PROJ/web", "--path", "src/other.py"]
    result = harness.run(*argv)
    assert result.code == 6
    assert result.error["code"] == "response_invalid"


def test_pr_diff_for_an_unchanged_file_is_not_found(harness: Harness) -> None:
    config(harness)
    diff = fixture("bitbucket_diff.json")
    diff["diffs"] = []
    harness.server.add("GET", DIFF + "/src/other.py", diff)
    argv = ["bitbucket", "pr", "diff", "42", "--repo", "PROJ/web", "--path", "src/other.py"]
    result = harness.run(*argv)
    assert result.error["code"] == "not_found"
    assert "yojo bitbucket pr diff 42 --repo PROJ/web" in result.error["fix"]


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "a//b", "a/./b"])
def test_pr_diff_path_is_repository_relative(harness: Harness, path: str) -> None:
    result = harness.run("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web", "--path", path)
    assert result.code == 2
    assert harness.server.requests == []


def test_pr_diff_too_large_suggests_one_file(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    config(harness)
    monkeypatch.setattr(transport, "MAX_RESPONSE_BYTES", 100)
    harness.server.add("GET", DIFF, fixture("bitbucket_diff.json"))
    result = harness.run("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web")
    assert result.code == 6
    assert "too large" in result.error["message"]
    assert "--path '<file path>'" in result.error["fix"]


def test_pr_diff_rejects_an_unknown_segment_type(harness: Harness) -> None:
    config(harness)
    diff = fixture("bitbucket_diff.json")
    diff["diffs"][0]["hunks"][0]["segments"][0]["type"] = "MOVED"
    harness.server.add("GET", DIFF, diff)
    result = harness.run("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web")
    assert result.error["code"] == "response_invalid"


# --------------------------------------------------------------------------- create


def test_create_in_one_call(harness: Harness) -> None:
    setup_create(harness)
    result = harness.run(*CREATE_ARGV, "--description", "Handles an empty user.")
    assert result.code == 0, result.out
    assert result.data == {
        "id": 42,
        "repo": "PROJ/web",
        "title": "Fix login on Safari",
        "source": "feature/login",
        "target": "main",
        "state": "OPEN",
        "url": f"{BITBUCKET}/projects/PROJ/repos/web/pull-requests/42",
    }
    (post,) = harness.server.writes()
    assert json.loads(post.content) == {
        "title": "Fix login on Safari",
        "description": "Handles an empty user.",
        "fromRef": {
            "id": "refs/heads/feature/login",
            "repository": {"slug": "web", "project": {"key": "PROJ"}},
        },
        "toRef": {
            "id": "refs/heads/main",
            "repository": {"slug": "web", "project": {"key": "PROJ"}},
        },
    }
    duplicate_scan = harness.server.requests[3].url.params
    assert (duplicate_scan["state"], duplicate_scan["at"], duplicate_scan["direction"]) == (
        "OPEN",
        "refs/heads/feature/login",
        "OUTGOING",
    )
    assert result.nbytes <= WRITE_BUDGET


@pytest.mark.parametrize("state", [None, "DECLINED", "MERGED", "open", 1])
def test_create_response_that_is_not_open_is_outcome_unknown(harness: Harness, state: Any) -> None:
    setup_create(harness)

    def not_open(request: httpx.Request) -> httpx.Response:
        pr = created_pr(request).json()
        if state is None:
            del pr["state"]
        else:
            pr["state"] = state
        return httpx.Response(201, json=pr)

    harness.server.routes[("POST", PRS)] = []
    harness.server.add("POST", PRS, handler=not_open)
    result = harness.run(*CREATE_ARGV)
    assert result.code == 6
    error = result.error
    assert error["code"] == "outcome_unknown"
    assert "open" in error["details"]["reason"]
    assert "retry" not in error["details"] and "retry_argv" not in error["details"]
    assert error["details"]["recovery_argv"]["get"][4] == "42"
    assert "yojo bitbucket pr get 42 --repo PROJ/web --full" in error["fix"]
    assert len(harness.server.writes()) == 1


def test_create_lists_every_problem_and_sends_nothing(harness: Harness) -> None:
    setup_create(harness)
    argv = list(CREATE_ARGV)
    argv[argv.index("feature/login")] = "feature/nope"
    argv[argv.index("Fix login on Safari")] = "   "
    details = refusal(harness.run(*argv))
    assert [(c["id"], c["status"]) for c in details["checks"]] == [
        ("title", "fail"),
        ("source", "fail"),
        ("duplicate", "unverified"),
    ]
    assert details["retry_argv"] == [
        "yojo",
        *argv[: argv.index("feature/nope")],
        "<branch>",
        "--target",
        "main",
        "--title",
        "<title>",
    ]
    assert "branch list --repo PROJ/web --filter feature/nope" in details["checks"][1]["fix"]
    assert harness.server.writes() == []


@pytest.mark.parametrize("failure", [403, 503, "timeout"])
@pytest.mark.parametrize("probe", ["repo", "branch", "duplicate"])
def test_create_probe_failure_preserves_local_checks(
    harness: Harness, failure: int | str, probe: str
) -> None:
    setup_create(harness)
    path = {"repo": REPO, "branch": BRANCHES, "duplicate": PRS}[probe]
    harness.server.routes[("GET", path)] = []
    if failure == "timeout":
        harness.server.add("GET", path, handler=_timeout)
    else:
        assert isinstance(failure, int)
        harness.server.add("GET", path, status=failure)
    argv = ["   " if arg == "Fix login on Safari" else arg for arg in CREATE_ARGV]
    details = refusal(harness.run(*argv))
    checks = {check["id"]: check["status"] for check in details["checks"]}
    assert checks["title"] == "fail"
    expected = {"repo": "repo", "branch": "source", "duplicate": "duplicate"}[probe]
    assert checks[expected] == "unverified"
    if probe == "repo":
        assert checks["target"] == "unverified"
        assert checks["duplicate"] == "unverified"
    assert harness.server.writes() == []


def test_create_suggests_a_near_miss_branch_and_corrects_the_retry(harness: Harness) -> None:
    setup_create(harness)
    argv = list(CREATE_ARGV)
    argv[argv.index("feature/login")] = "feature/logn"
    details = refusal(harness.run(*argv))
    (check,) = [c for c in details["checks"] if c["id"] == "source"]
    assert "Did you mean 'feature/login'?" in check["message"]
    assert details["retry_argv"] == ["yojo", *CREATE_ARGV]
    assert harness.server.writes() == []
    assert harness.run(*details["retry_argv"][1:]).code == 0


def test_create_prefers_exact_branch_and_lists_ambiguous_ones(harness: Harness) -> None:
    setup_create(harness)
    branches = fixture("bitbucket_branches.json")["values"]
    twins = [
        {**branches[1], "id": "refs/heads/Feature/Login", "displayId": "Feature/Login"},
        branches[1],
    ]
    harness.server.routes[("GET", BRANCHES)] = []
    harness.server.add("GET", BRANCHES, page(twins))
    argv = list(CREATE_ARGV)
    argv[argv.index("feature/login")] = "FEATURE/LOGIN"
    details = refusal(harness.run(*argv))
    source = next(c for c in details["checks"] if c["id"] == "source")
    assert source["details"]["kind"] == "ambiguous"
    assert sorted(source["details"]["candidates"]) == ["Feature/Login", "feature/login"]
    assert harness.server.writes() == []


def test_create_refuses_a_branch_whose_id_is_not_its_name(harness: Harness) -> None:
    setup_create(harness)
    branches = fixture("bitbucket_branches.json")["values"]
    harness.server.routes[("GET", BRANCHES)] = []
    harness.server.add("GET", BRANCHES, page([{**branches[1], "id": "refs/heads/release"}]))
    harness.server.add("GET", BRANCHES, page([branches[0]]))
    details = refusal(harness.run(*CREATE_ARGV))
    source = next(c for c in details["checks"] if c["id"] == "source")
    assert source["status"] == "unverified"
    assert harness.server.writes() == []


def test_create_refuses_an_inaccessible_repository(harness: Harness) -> None:
    config(harness)
    harness.server.add(
        "GET", REPO, {"errors": [{"message": "Repository does not exist"}]}, status=404
    )
    details = refusal(harness.run(*CREATE_ARGV))
    assert [(c["id"], c["status"]) for c in details["checks"]] == [
        ("repo", "fail"),
        ("source", "unverified"),
        ("target", "unverified"),
        ("duplicate", "unverified"),
    ]
    assert "'<PROJECT/slug>'" in details["retry"]
    assert harness.server.paths() == [REPO]


def test_create_refuses_an_existing_open_pull_request(harness: Harness) -> None:
    setup_create(harness, open_prs=[fixture("bitbucket_pull_request.json")])
    details = refusal(harness.run(*CREATE_ARGV))
    (check,) = details["checks"]
    assert check["id"] == "duplicate"
    assert "yojo bitbucket pr get 42 --repo PROJ/web" in check["fix"]
    assert details["retry"] is None and details["retry_argv"] is None
    assert harness.server.writes() == []


def test_create_refuses_an_open_pull_request_on_page_two(harness: Harness) -> None:
    setup_create(harness)
    other = fixture("bitbucket_pull_request.json")
    other["id"] = 41
    other["toRef"]["id"] = "refs/heads/release"
    harness.server.routes[("GET", PRS)] = []
    harness.server.add("GET", PRS, page([other], next_start=1))
    harness.server.add("GET", PRS, page([fixture("bitbucket_pull_request.json")], start=1))
    details = refusal(harness.run(*CREATE_ARGV))
    (check,) = details["checks"]
    assert check["id"] == "duplicate"
    assert "yojo bitbucket pr get 42 --repo PROJ/web" in check["fix"]
    assert [r.url.params["start"] for r in harness.server.requests if r.url.path == PRS] == [
        "0",
        "1",
    ]
    assert harness.server.writes() == []


def test_create_refuses_same_source_and_target(harness: Harness) -> None:
    setup_create(harness)
    argv = list(CREATE_ARGV)
    argv[argv.index("feature/login")] = "refs/heads/main"
    details = refusal(harness.run(*argv))
    assert [c["id"] for c in details["checks"]] == ["same_branch"]
    assert harness.server.writes() == []


def test_create_refuses_when_the_duplicate_scan_cannot_finish(harness: Harness) -> None:
    setup_create(harness)
    harness.server.routes[("GET", PRS)] = []
    harness.server.add("GET", PRS, {"size": 0, "isLastPage": False, "start": 0, "values": []})
    details = refusal(harness.run(*CREATE_ARGV))
    (check,) = details["checks"]
    assert (check["id"], check["status"]) == ("duplicate", "unverified")
    assert harness.server.writes() == []


@pytest.mark.parametrize("echoed", [None, "1", 0])
def test_create_refuses_when_a_later_page_does_not_echo_its_start(
    harness: Harness, echoed: Any
) -> None:
    setup_create(harness)
    other = fixture("bitbucket_pull_request.json")
    other["id"] = 41
    other["toRef"]["id"] = "refs/heads/release"
    later = page([], start=1)
    if echoed is None:
        del later["start"]
    else:
        later["start"] = echoed
    harness.server.routes[("GET", PRS)] = []
    harness.server.add("GET", PRS, page([other], next_start=1))
    harness.server.add("GET", PRS, later)
    details = refusal(harness.run(*CREATE_ARGV))
    (check,) = details["checks"]
    assert (check["id"], check["status"]) == ("duplicate", "unverified")
    assert harness.server.writes() == []


@pytest.mark.parametrize("on_later_page", [False, True])
def test_create_refuses_when_a_page_size_does_not_count_its_values(
    harness: Harness, on_later_page: bool
) -> None:
    setup_create(harness)
    other = fixture("bitbucket_pull_request.json")
    other["id"] = 41
    other["toRef"]["id"] = "refs/heads/release"
    harness.server.routes[("GET", PRS)] = []
    if on_later_page:
        harness.server.add("GET", PRS, page([other], next_start=1))
        harness.server.add("GET", PRS, page([], start=1) | {"size": 1})
    else:
        harness.server.add("GET", PRS, page([other]) | {"size": 2})
    details = refusal(harness.run(*CREATE_ARGV))
    (check,) = details["checks"]
    assert (check["id"], check["status"]) == ("duplicate", "unverified")
    assert harness.server.writes() == []


@pytest.mark.parametrize("on_later_page", [False, True])
@pytest.mark.parametrize("side", ["fromRef", "toRef"])
def test_create_refuses_when_an_open_pull_request_lacks_refs(
    harness: Harness, on_later_page: bool, side: str
) -> None:
    setup_create(harness)
    other = fixture("bitbucket_pull_request.json")
    other["id"] = 41
    other["toRef"]["id"] = "refs/heads/release"
    broken = fixture("bitbucket_pull_request.json")
    del broken[side]
    harness.server.routes[("GET", PRS)] = []
    if on_later_page:
        harness.server.add("GET", PRS, page([other], next_start=1))
        harness.server.add("GET", PRS, page([broken], start=1))
    else:
        harness.server.add("GET", PRS, page([broken, other]))
    details = refusal(harness.run(*CREATE_ARGV))
    (check,) = details["checks"]
    assert (check["id"], check["status"]) == ("duplicate", "unverified")
    assert "without the details needed" in check["message"]
    assert harness.server.writes() == []


def test_create_without_repo_is_refused_before_any_request(harness: Harness) -> None:
    details = refusal(harness.run("bitbucket", "pr", "create", "--title", "x"))
    assert [(c["id"], c["status"]) for c in details["checks"]] == [
        ("repo", "fail"),
        ("source", "fail"),
        ("target", "fail"),
        ("duplicate", "unverified"),
    ]
    assert details["retry_argv"] == [
        "yojo",
        "bitbucket",
        "pr",
        "create",
        "--title",
        "x",
        "--repo",
        "<PROJECT/slug>",
        "--source",
        "<branch>",
        "--target",
        "<branch>",
    ]
    assert harness.server.requests == []


def test_create_from_input(harness: Harness) -> None:
    setup_create(harness)
    doc = {
        "repo": "PROJ/web",
        "source": "feature/login",
        "target": "main",
        "title": "Fix login on Safari",
        "description": "Line one\nLine two",
    }
    result = harness.run(
        "bitbucket", "pr", "create", "--input", "-", stdin=json.dumps(doc).encode()
    )
    assert result.code == 0, result.out
    assert json.loads(harness.server.writes()[0].content)["description"] == "Line one\nLine two"


def test_create_input_problems_get_no_retry_line(harness: Harness) -> None:
    setup_create(harness)
    doc = {"repo": "PROJ/web", "source": "feature/login", "target": "main", "title": "  "}
    details = refusal(
        harness.run("bitbucket", "pr", "create", "--input", "-", stdin=json.dumps(doc).encode())
    )
    assert [c["id"] for c in details["checks"]] == ["title"]
    assert details["retry"] is None
    assert harness.server.writes() == []


def test_create_flag_and_input_conflict_sends_nothing(harness: Harness) -> None:
    setup_create(harness)
    doc = {"repo": "PROJ/web", "title": "One"}
    result = harness.run(
        "bitbucket",
        "pr",
        "create",
        "--title",
        "Two",
        "--input",
        "-",
        stdin=json.dumps(doc).encode(),
    )
    assert result.code == 2
    assert result.error["details"]["conflicts"] == ["title"]
    assert harness.server.requests == []


def test_create_server_rejection_shows_bitbucket_errors(harness: Harness) -> None:
    setup_create(harness)
    harness.server.routes[("POST", PRS)] = []
    harness.server.add("POST", PRS, fixture("bitbucket_error_duplicate.json"), status=409)
    result = harness.run(*CREATE_ARGV)
    assert result.code == 5
    error = result.error
    assert error["code"] == "conflict"
    assert error["details"]["messages"] == [
        "Only one pull request may be open for a given source and target branch"
    ]
    assert "retry" not in error["details"]
    assert len(harness.server.writes()) == 1


def test_create_validation_error_names_the_field(harness: Harness) -> None:
    setup_create(harness)
    harness.server.routes[("POST", PRS)] = []
    harness.server.add(
        "POST",
        PRS,
        {"errors": [{"context": "title", "message": "Title is too long"}]},
        status=400,
    )
    result = harness.run(*CREATE_ARGV)
    assert result.error["code"] == "remote_validation"
    assert result.error["details"]["fields"] == {"title": "Title is too long"}


def test_uncertain_create_offers_inspection_not_a_retry(harness: Harness) -> None:
    setup_create(harness)
    harness.server.routes[("POST", PRS)] = []
    harness.server.add("POST", PRS, handler=_timeout)
    result = harness.run(*CREATE_ARGV)
    assert result.code == 6
    error = result.error
    assert error["code"] == "outcome_unknown"
    assert "retry" not in error["details"] and "retry_argv" not in error["details"]
    assert error["details"]["recovery_argv"] == {
        "list": ["yojo", "bitbucket", "pr", "list", "--repo", "PROJ/web", "--state", "ALL"]
    }
    assert "Do not retry yet" in error["fix"]
    assert "every page" in error["fix"]
    assert "only to find candidates" in error["fix"]
    assert "yojo bitbucket pr get ID --repo PROJ/web --full" in error["fix"]
    assert "created at or after details.sent_at" in error["fix"]
    assert "merged or declined" in error["fix"]
    assert "ask a person to confirm" in error["fix"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", error["details"]["sent_at"])
    assert len(harness.server.writes()) == 1
    assert result.nbytes <= WRITE_BUDGET


@pytest.mark.parametrize("status", [302, 307])
def test_redirected_create_is_outcome_unknown(harness: Harness, status: int) -> None:
    setup_create(harness)
    harness.server.routes[("POST", PRS)] = []
    location = BITBUCKET + "/login?next=pull-requests"
    harness.server.add("POST", PRS, status=status, headers={"location": location})
    result = harness.run(*CREATE_ARGV)
    assert result.code == 6
    error = result.error
    assert error["code"] == "outcome_unknown"
    assert "retry" not in error["details"] and "retry_argv" not in error["details"]
    assert error["details"]["recovery_argv"] == {
        "list": ["yojo", "bitbucket", "pr", "list", "--repo", "PROJ/web", "--state", "ALL"]
    }
    assert "Do not retry yet" in error["fix"]
    assert [r.method for r in harness.server.writes()] == ["POST"]
    assert "/login" not in harness.server.paths()


def test_unconfirmed_create_is_outcome_unknown(harness: Harness) -> None:
    setup_create(harness)
    wrong = fixture("bitbucket_pull_request.json")
    wrong["toRef"]["id"] = "refs/heads/release"
    harness.server.routes[("POST", PRS)] = []
    harness.server.add("POST", PRS, wrong, status=201)
    result = harness.run(*CREATE_ARGV)
    assert result.error["code"] == "outcome_unknown"
    assert "target branch" in result.error["details"]["reason"]
    assert result.error["details"]["recovery_argv"]["get"] == [
        "yojo",
        "bitbucket",
        "pr",
        "get",
        "42",
        "--repo",
        "PROJ/web",
        "--full",
    ]
    assert "yojo bitbucket pr get 42 --repo PROJ/web --full" in result.error["fix"]
    assert "sent_at" in result.error["details"]


def test_create_response_with_changed_title_whitespace_is_outcome_unknown(harness: Harness) -> None:
    setup_create(harness)
    wrong = fixture("bitbucket_pull_request.json")
    wrong["title"] = "Fix login on Safari "
    harness.server.routes[("POST", PRS)] = []
    harness.server.add("POST", PRS, wrong, status=201)
    result = harness.run(*CREATE_ARGV)
    assert result.code == 6
    assert result.error["code"] == "outcome_unknown"
    assert len(harness.server.writes()) == 1


@pytest.mark.parametrize(
    "returned",
    [None, "", "Handles an empty user. ", "Something else", {"text": "Handles an empty user."}],
)
def test_create_response_with_changed_description_is_outcome_unknown(
    harness: Harness, returned: Any
) -> None:
    setup_create(harness)
    response = fixture("bitbucket_pull_request.json")
    response["title"] = "Fix login on Safari"
    if returned is None:
        del response["description"]
    else:
        response["description"] = returned
    harness.server.routes[("POST", PRS)] = []
    harness.server.add("POST", PRS, response, status=201)
    result = harness.run(*CREATE_ARGV, "--description", "Handles an empty user.")
    assert result.code == 6
    assert result.error["code"] == "outcome_unknown"
    assert "description" in result.error["details"]["reason"]
    assert len(harness.server.writes()) == 1


def test_create_terminal_and_json_share_exit_codes(harness: Harness) -> None:
    setup_create(harness)
    tty = harness.run_tty(*CREATE_ARGV, environ={"NO_COLOR": "1"})
    assert tty.code == 0
    assert tty.stdout.startswith("Opened pull request #42 in PROJ/web: Fix login on Safari")
    assert "\x1b" not in tty.stdout
    harness.server.routes[("GET", PRS)] = []
    harness.server.add("GET", PRS, page([fixture("bitbucket_pull_request.json")]))
    refused_tty = harness.run_tty(*CREATE_ARGV)
    refused = harness.run(*CREATE_ARGV)
    assert refused_tty.code == refused.code == 4
    assert "nothing was sent" in refused_tty.stdout


# --------------------------------------------------------------------------- comment


def test_comment(harness: Harness) -> None:
    setup_comment(harness)
    result = harness.run(*COMMENT_ARGV, "--body", "Ready for review.")
    assert result.code == 0, result.out
    assert result.data == {
        "id": 42,
        "repo": "PROJ/web",
        "comment_id": 777,
        "url": f"{BITBUCKET}/projects/PROJ/repos/web/pull-requests/42/overview?commentId=777",
    }
    (post,) = harness.server.writes()
    assert json.loads(post.content) == {"text": "Ready for review."}
    assert result.nbytes <= WRITE_BUDGET


def test_comment_from_file(harness: Harness) -> None:
    setup_comment(harness)
    (harness.tmp_path / "note.txt").write_bytes(b"Ready for review.\n")
    harness.server.routes[("POST", COMMENTS)] = []
    response = fixture("bitbucket_comment.json")
    response["text"] = "Ready for review.\n"
    harness.server.add("POST", COMMENTS, response, status=201)
    result = harness.run(*COMMENT_ARGV, "--body-file", "note.txt")
    assert result.code == 0, result.out


def test_comment_refuses_an_identical_comment(harness: Harness) -> None:
    setup_comment(harness)
    details = refusal(harness.run(*COMMENT_ARGV, "--body", "Looks good overall."))
    (check,) = details["checks"]
    assert check["id"] == "duplicate"
    assert check["details"] == {"comment_id": 501}
    assert details["retry"] is None
    assert harness.server.writes() == []


def test_comment_with_distinct_trailing_space_is_not_a_duplicate(harness: Harness) -> None:
    setup_comment(harness)
    harness.server.routes[("POST", COMMENTS)] = []
    response = fixture("bitbucket_comment.json")
    response["text"] = "Looks good overall. "
    harness.server.add("POST", COMMENTS, response, status=201)
    result = harness.run(*COMMENT_ARGV, "--body", "Looks good overall. ")
    assert result.code == 0, result.out
    assert len(harness.server.writes()) == 1


def test_comment_refuses_blank_body_and_missing_pr_together(harness: Harness) -> None:
    config(harness)
    harness.server.add("GET", PR, {"errors": [{"message": "No such pull request"}]}, status=404)
    details = refusal(harness.run(*COMMENT_ARGV, "--body", "  "))
    assert [c["id"] for c in details["checks"]] == ["body", "pull_request"]
    assert harness.server.writes() == []


@pytest.mark.parametrize("failure", [403, 503, "timeout"])
def test_comment_pr_probe_failure_keeps_blank_body(harness: Harness, failure: int | str) -> None:
    setup_comment(harness)
    harness.server.routes[("GET", PR)] = []
    if failure == "timeout":
        harness.server.add("GET", PR, handler=_timeout)
    else:
        assert isinstance(failure, int)
        harness.server.add("GET", PR, status=failure)
    details = refusal(harness.run(*COMMENT_ARGV, "--body", "  "))
    assert [(check["id"], check["status"]) for check in details["checks"]] == [
        ("body", "fail"),
        ("pull_request", "unverified"),
    ]
    assert harness.server.writes() == []


@pytest.mark.parametrize("failure", [403, 503, "timeout"])
def test_comment_activity_probe_failure_refuses_post(harness: Harness, failure: int | str) -> None:
    setup_comment(harness)
    harness.server.routes[("GET", ACTIVITIES)] = []
    if failure == "timeout":
        harness.server.add("GET", ACTIVITIES, handler=_timeout)
    else:
        assert isinstance(failure, int)
        harness.server.add("GET", ACTIVITIES, status=failure)
    details = refusal(harness.run(*COMMENT_ARGV, "--body", "New note"))
    assert [(check["id"], check["status"]) for check in details["checks"]] == [
        ("duplicate", "unverified")
    ]
    assert harness.server.writes() == []


def test_comment_refuses_when_activity_scan_is_incomplete(harness: Harness) -> None:
    setup_comment(harness)
    harness.server.routes[("GET", ACTIVITIES)] = []
    harness.server.add("GET", ACTIVITIES, page([], next_start=0))
    details = refusal(harness.run(*COMMENT_ARGV, "--body", "New note"))
    assert details["checks"][0]["status"] == "unverified"
    assert harness.server.writes() == []


@pytest.mark.parametrize("echoed", [None, "1", 0])
def test_comment_refuses_when_a_later_page_does_not_echo_its_start(
    harness: Harness, echoed: Any
) -> None:
    setup_comment(harness)
    later = page([], start=1)
    if echoed is None:
        del later["start"]
    else:
        later["start"] = echoed
    harness.server.routes[("GET", ACTIVITIES)] = []
    harness.server.add("GET", ACTIVITIES, page([{"action": "OPENED"}], next_start=1))
    harness.server.add("GET", ACTIVITIES, later)
    details = refusal(harness.run(*COMMENT_ARGV, "--body", "New note"))
    (check,) = details["checks"]
    assert (check["id"], check["status"]) == ("duplicate", "unverified")
    assert harness.server.writes() == []


@pytest.mark.parametrize("on_later_page", [False, True])
@pytest.mark.parametrize(
    "broken",
    [
        {"action": "COMMENTED", "comment": {"id": 502}},
        {"action": "COMMENTED"},
        {"comment": {"id": 502, "text": "x"}},
        None,
    ],
)
def test_comment_refuses_when_an_activity_cannot_be_compared(
    harness: Harness, on_later_page: bool, broken: Any
) -> None:
    setup_comment(harness)
    harness.server.routes[("GET", ACTIVITIES)] = []
    if on_later_page:
        harness.server.add("GET", ACTIVITIES, page([{"action": "OPENED"}], next_start=1))
        harness.server.add("GET", ACTIVITIES, page([broken], start=1))
    else:
        harness.server.add("GET", ACTIVITIES, page([broken, {"action": "OPENED"}]))
    details = refusal(harness.run(*COMMENT_ARGV, "--body", "New note"))
    (check,) = details["checks"]
    assert (check["id"], check["status"]) == ("duplicate", "unverified")
    assert harness.server.writes() == []


@pytest.mark.parametrize("on_later_page", [False, True])
def test_comment_refuses_when_a_page_size_does_not_count_its_values(
    harness: Harness, on_later_page: bool
) -> None:
    setup_comment(harness)
    harness.server.routes[("GET", ACTIVITIES)] = []
    if on_later_page:
        harness.server.add("GET", ACTIVITIES, page([{"action": "OPENED"}], next_start=1))
        harness.server.add("GET", ACTIVITIES, page([], start=1) | {"size": 1})
    else:
        harness.server.add("GET", ACTIVITIES, page([{"action": "OPENED"}]) | {"size": 2})
    details = refusal(harness.run(*COMMENT_ARGV, "--body", "New note"))
    (check,) = details["checks"]
    assert (check["id"], check["status"]) == ("duplicate", "unverified")
    assert harness.server.writes() == []


def _with_reply(text: str) -> dict[str, Any]:
    activities: dict[str, Any] = fixture("bitbucket_activities.json")
    comment = activities["values"][0]["comment"]
    reply = {**copy.deepcopy(comment), "id": 503, "text": "Thanks.", "comments": []}
    reply["comments"] = [{**copy.deepcopy(comment), "id": 504, "text": text, "comments": []}]
    comment["comments"] = [reply]
    return activities


def test_comment_refuses_an_identical_nested_reply(harness: Harness) -> None:
    setup_comment(harness)
    harness.server.routes[("GET", ACTIVITIES)] = []
    harness.server.add("GET", ACTIVITIES, _with_reply("Fixed in the next commit."))
    details = refusal(harness.run(*COMMENT_ARGV, "--body", "Fixed in the next commit."))
    (check,) = details["checks"]
    assert (check["id"], check["status"]) == ("duplicate", "fail")
    assert check["details"] == {"comment_id": 504}
    assert harness.server.writes() == []


@pytest.mark.parametrize("broken", [{"id": 505}, None, {"id": 505, "text": "x", "comments": {}}])
def test_comment_refuses_when_a_nested_reply_cannot_be_compared(
    harness: Harness, broken: Any
) -> None:
    setup_comment(harness)
    activities = _with_reply("Thanks again.")
    activities["values"][0]["comment"]["comments"][0]["comments"].append(broken)
    harness.server.routes[("GET", ACTIVITIES)] = []
    harness.server.add("GET", ACTIVITIES, activities)
    details = refusal(harness.run(*COMMENT_ARGV, "--body", "New note"))
    (check,) = details["checks"]
    assert (check["id"], check["status"]) == ("duplicate", "unverified")
    assert harness.server.writes() == []


@pytest.mark.parametrize("state", ["MERGED", "DECLINED"])
def test_comment_on_merged_or_declined_pull_request(harness: Harness, state: str) -> None:
    setup_comment(harness)
    harness.server.routes[("GET", PR)] = []
    harness.server.add("GET", PR, {**fixture("bitbucket_pull_request.json"), "state": state})
    result = harness.run(*COMMENT_ARGV, "--body", "Ready for review.")
    assert result.code == 0, result.out
    assert len(harness.server.writes()) == 1


@pytest.mark.parametrize("state", [None, "SUPERSEDED", 3])
def test_comment_refuses_when_the_pull_request_state_is_unknown(
    harness: Harness, state: Any
) -> None:
    setup_comment(harness)
    pr = fixture("bitbucket_pull_request.json")
    if state is None:
        del pr["state"]
    else:
        pr["state"] = state
    harness.server.routes[("GET", PR)] = []
    harness.server.add("GET", PR, pr)
    details = refusal(harness.run(*COMMENT_ARGV, "--body", "  "))
    assert [(c["id"], c["status"]) for c in details["checks"]] == [
        ("body", "fail"),
        ("pull_request", "unverified"),
    ]
    assert "yojo bitbucket pr get 42 --repo PROJ/web" in details["checks"][1]["fix"]
    assert harness.server.writes() == []


@pytest.mark.parametrize(
    ("status", "code", "exit_code"),
    [(400, "remote_validation", 5), (403, "forbidden", 3), (409, "conflict", 5)],
)
def test_comment_server_rejection_is_definite(
    harness: Harness, status: int, code: str, exit_code: int
) -> None:
    setup_comment(harness)
    harness.server.routes[("POST", COMMENTS)] = []
    harness.server.add(
        "POST", COMMENTS, {"errors": [{"message": "Comments are locked"}]}, status=status
    )
    result = harness.run(*COMMENT_ARGV, "--body", "Ready for review.")
    assert result.code == exit_code, result.out
    error = result.error
    assert error["code"] == code
    assert error["details"]["status"] == status
    assert "Comments are locked" in error["details"]["messages"]
    assert not {"retry", "retry_argv", "recovery_argv"} & error["details"].keys()
    assert len(harness.server.writes()) == 1
    assert harness.server.requests[-1].method == "POST"


def test_comment_scans_every_activity_page(harness: Harness) -> None:
    setup_comment(harness)
    first = page([{"action": "OPENED"}], next_start=1)
    second = fixture("bitbucket_activities.json") | {"start": 1}
    harness.server.routes[("GET", ACTIVITIES)] = []
    harness.server.add("GET", ACTIVITIES, first)
    harness.server.add("GET", ACTIVITIES, second)
    details = refusal(harness.run(*COMMENT_ARGV, "--body", "Looks good overall."))
    assert details["checks"][0]["id"] == "duplicate"
    assert [r.url.params["start"] for r in harness.server.requests[1:]] == ["0", "1"]


def test_unconfirmed_comment_is_outcome_unknown(harness: Harness) -> None:
    setup_comment(harness)
    harness.server.routes[("POST", COMMENTS)] = []
    harness.server.add("POST", COMMENTS, {"id": 777, "text": "something else"}, status=201)
    result = harness.run(*COMMENT_ARGV, "--body", "Ready for review.")
    assert result.code == 6
    error = result.error
    assert error["code"] == "outcome_unknown"
    assert "retry" not in error["details"]
    assert error["details"]["recovery_argv"]["pull_request"][:4] == [
        "yojo",
        "bitbucket",
        "pr",
        "get",
    ]
    assert "/pull-requests/42/overview" in error["fix"]


def test_comment_timeout_offers_inspection_not_a_retry(harness: Harness) -> None:
    setup_comment(harness)
    harness.server.routes[("POST", COMMENTS)] = []
    harness.server.add("POST", COMMENTS, handler=_timeout)
    result = harness.run(*COMMENT_ARGV, "--body", "Ready for review.")
    assert result.code == 6
    error = result.error
    assert error["code"] == "outcome_unknown"
    assert "retry" not in error["details"] and "retry_argv" not in error["details"]
    assert error["details"]["recovery_argv"] == {
        "pull_request": ["yojo", "bitbucket", "pr", "get", "42", "--repo", "PROJ/web"]
    }
    assert "/pull-requests/42/overview" in error["fix"]
    assert "Do not retry yet" in error["fix"]
    assert len(harness.server.writes()) == 1
    assert result.nbytes <= WRITE_BUDGET


@pytest.mark.parametrize("status", [302, 307])
def test_redirected_comment_is_outcome_unknown(harness: Harness, status: int) -> None:
    setup_comment(harness)
    harness.server.routes[("POST", COMMENTS)] = []
    location = BITBUCKET + "/login?next=comments"
    harness.server.add("POST", COMMENTS, status=status, headers={"location": location})
    result = harness.run(*COMMENT_ARGV, "--body", "Ready for review.")
    assert result.code == 6
    error = result.error
    assert error["code"] == "outcome_unknown"
    assert "retry" not in error["details"] and "retry_argv" not in error["details"]
    assert error["details"]["recovery_argv"] == {
        "pull_request": ["yojo", "bitbucket", "pr", "get", "42", "--repo", "PROJ/web"]
    }
    assert "Do not retry yet" in error["fix"]
    assert [r.method for r in harness.server.writes()] == ["POST"]
    assert "/login" not in harness.server.paths()


def test_comment_response_with_changed_whitespace_is_outcome_unknown(harness: Harness) -> None:
    setup_comment(harness)
    result = harness.run(*COMMENT_ARGV, "--body", "Ready for review. ")
    assert result.code == 6
    assert result.error["code"] == "outcome_unknown"
    assert len(harness.server.writes()) == 1


def test_comment_body_file_must_be_utf8(harness: Harness) -> None:
    setup_comment(harness)
    (harness.tmp_path / "bad.txt").write_bytes(b"\xff\xfe\x00")
    result = harness.run(*COMMENT_ARGV, "--body-file", "bad.txt")
    assert result.code == 2
    assert harness.server.requests == []


# --------------------------------------------------------------------------- boundaries


def test_no_approve_merge_or_decline_command() -> None:
    words = {word for c in COMMANDS for word in c.path.split()}
    assert not words & {"approve", "merge", "decline", "unapprove"}


@pytest.mark.parametrize("verb", ["approve", "merge", "decline"])
def test_approve_and_merge_are_not_reachable(harness: Harness, verb: str) -> None:
    config(harness)
    result = harness.run("bitbucket", "pr", verb, "42", "--repo", "PROJ/web")
    assert result.code == 2
    assert result.error["code"] == "usage_error"
    assert harness.run("schema", "bitbucket", "pr", verb).code == 2
    assert harness.server.requests == []


def test_repo_list_default_page_fits_the_read_budget(harness: Harness) -> None:
    config(harness)
    repo = fixture("bitbucket_repo.json")
    repos = [
        {**copy.deepcopy(repo), "slug": f"service-{n:02d}", "name": f"Service {n:02d}"}
        for n in range(20)
    ]
    harness.server.add("GET", REPOS, page(repos, next_start=20))
    result = harness.run("bitbucket", "repo", "list")
    assert len(result.data["repos"]) == 20
    assert result.nbytes <= READ_BUDGET


def test_oversized_repo_page_offers_a_smaller_limit(harness: Harness) -> None:
    config(harness)
    repo = fixture("bitbucket_repo.json")
    repos = [
        {**copy.deepcopy(repo), "slug": f"service-{n:02d}", "name": "N" * 255} for n in range(20)
    ]
    harness.server.add("GET", REPOS, page(repos))
    result = harness.run("bitbucket", "repo", "list")
    assert result.code == 5
    assert result.error["code"] == "remote_limit_exceeded"
    assert "--limit 10" in result.error["fix"]
    assert result.nbytes <= READ_BUDGET
