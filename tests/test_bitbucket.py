"""Bitbucket Data Center: login, reads, cursors, diff limits, and checked writes.

Every response is a fixture modeled on the Data Center REST 1.0 reference; no test reaches a
real server or keyring. A refused write must send zero POSTs; an uncertain write must offer an
inspection, never a retry.
"""

from __future__ import annotations

import copy
import json
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
DEFAULT_BRANCH = REPO + "/branches/default"
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
        {"isLastPage": False, "start": 0, "values": []},
        {"isLastPage": False, "start": 0, "nextPageStart": 0, "values": []},
        {"isLastPage": False, "start": 0, "nextPageStart": "20", "values": []},
        {"start": 0, "values": []},
        {"isLastPage": True, "start": 40, "values": []},
        {"isLastPage": True, "start": 0, "values": [{}] * 21},
        {"isLastPage": True, "start": 0, "values": "nope"},
    ],
    ids=["missing", "repeating", "not-int", "no-last-flag", "wrong-start", "too-many", "values"],
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


def test_pr_get_rejects_another_pull_request(harness: Harness) -> None:
    config(harness)
    harness.server.add("GET", PR, {**fixture("bitbucket_pull_request.json"), "id": 43})
    assert harness.run("bitbucket", "pr", "get", "42", "--repo", "PROJ/web").code == 6


# --------------------------------------------------------------------------- diff


def test_pr_diff_is_unified_and_complete_when_small(harness: Harness) -> None:
    config(harness)
    harness.server.add("GET", DIFF, fixture("bitbucket_diff.json"))
    result = harness.run("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web")
    data = result.data
    assert data["complete"] is True and data["more"] is None
    assert data["files"] == [
        {"path": "src/app.py", "old_path": None, "change": "modified", "added": 2, "removed": 1},
        {"path": "docs/login.md", "old_path": None, "change": "added", "added": 1, "removed": 0},
    ]
    assert data["diff"].splitlines()[:6] == [
        "diff --git a/src/app.py b/src/app.py",
        "--- a/src/app.py",
        "+++ b/src/app.py",
        "@@ -10,2 +10,3 @@",
        " def login(user):",
        "-    return check(user)",
    ]
    assert "--- /dev/null" in data["diff"]
    assert harness.server.requests[0].url.params["withComments"] == "false"


def test_pr_diff_marks_local_truncation(harness: Harness) -> None:
    config(harness)
    diff = fixture("bitbucket_diff.json")
    lines = diff["diffs"][0]["hunks"][0]["segments"][2]["lines"]
    lines += [{"line": f"    step_{n} = {n}", "truncated": False} for n in range(400)]
    harness.server.add("GET", DIFF, diff)
    data = harness.run("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web").data
    assert data["truncated"] is True and data["complete"] is False
    assert data["diff"].endswith("\n")
    assert len(data["diff"]) < data["diff_chars"]
    assert data["more"] == "yojo bitbucket pr diff 42 --repo PROJ/web --full"
    full = harness.run("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web", "--full").data
    assert full["complete"] is True
    assert len(full["diff"]) == full["diff_chars"]


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
    tty = harness.run_tty("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web", "--full")
    assert "Incomplete: Bitbucket cut part of the diff." in tty.stdout


def test_pr_diff_for_one_file(harness: Harness) -> None:
    config(harness)
    diff = fixture("bitbucket_diff.json")
    diff["diffs"] = diff["diffs"][1:]
    harness.server.add("GET", DIFF + "/docs/login notes.md", diff)
    result = harness.run(
        "bitbucket", "pr", "diff", "42", "--repo", "PROJ/web", "--path", "docs/login notes.md"
    )
    assert result.code == 0, result.out
    assert result.data["path"] == "docs/login notes.md"
    assert harness.server.requests[0].url.raw_path.startswith(
        b"/bitbucket/rest/api/1.0/projects/PROJ/repos/web/pull-requests/42/diff/docs/login%20notes.md"
    )
    assert result.data["url"].endswith("/pull-requests/42/diff#docs/login%20notes.md")


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
    harness.server.add("GET", PRS, {"isLastPage": False, "start": 0, "values": []})
    details = refusal(harness.run(*CREATE_ARGV))
    (check,) = details["checks"]
    assert (check["id"], check["status"]) == ("duplicate", "unverified")
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
        "list": ["yojo", "bitbucket", "pr", "list", "--repo", "PROJ/web"]
    }
    assert "Do not retry yet" in error["fix"]
    assert len(harness.server.writes()) == 1
    assert result.nbytes <= WRITE_BUDGET


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
    ]


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


def test_comment_refuses_when_activity_scan_is_incomplete(harness: Harness) -> None:
    setup_comment(harness)
    harness.server.routes[("GET", ACTIVITIES)] = []
    harness.server.add("GET", ACTIVITIES, page([], next_start=0))
    details = refusal(harness.run(*COMMENT_ARGV, "--body", "New note"))
    assert details["checks"][0]["status"] == "unverified"
    assert harness.server.writes() == []


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
        {**copy.deepcopy(repo), "slug": f"service-{n:02d}", "name": "N" * 255}
        for n in range(20)
    ]
    harness.server.add("GET", REPOS, page(repos))
    result = harness.run("bitbucket", "repo", "list")
    assert result.code == 5
    assert result.error["code"] == "remote_limit_exceeded"
    assert "--limit 10" in result.error["fix"]
    assert result.nbytes <= READ_BUDGET
