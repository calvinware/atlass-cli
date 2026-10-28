"""Regression cases for the issue #15 Bitbucket review follow-ups."""

from __future__ import annotations

import copy
import json
import shlex
from typing import Any

import httpx
import pytest

from tests.conftest import BITBUCKET, JIRA, Harness, fixture
from tests.test_bitbucket import (
    ACTIVITIES,
    BRANCHES,
    COMMENT_ARGV,
    COMMENTS,
    CREATE_ARGV,
    DIFF,
    PR,
    PRS,
    REPOS,
    config,
    page,
    refusal,
    setup_comment,
    setup_create,
)
from yojo.auth import Credential, CredentialStore
from yojo.cli import build_parser


def test_config_check_accepts_confluence_only(harness: Harness) -> None:
    harness.write_config("version: 1\nconfluence:\n  base_url: https://confluence.example.test\n")
    result = harness.run("config", "check")
    assert result.code == 0, result.out
    assert result.data["checks"][0]["id"] == "confluence:config"
    assert harness.server.requests == []
    wrong_url = harness.run("config", "check", "--base-url", "https://wrong.example.test")
    assert wrong_url.error["code"] == "input_invalid"


def test_config_check_collects_independent_bitbucket_result(harness: Harness) -> None:
    harness.write_config(
        f"version: 1\njira:\n  base_url: {JIRA}\nbitbucket:\n  base_url: {BITBUCKET}\n"
    )
    harness.store("bitbucket")
    from tests.test_bitbucket import PROPS, RECENT

    harness.server.add("GET", PROPS, fixture("bitbucket_application_properties.json"))
    harness.server.add("GET", RECENT, page([]))
    result = harness.run("config", "check")
    assert result.error["code"] == "config_unverified"
    checks = result.error["details"]["checks"]
    assert any(c["id"] == "jira:check" for c in checks)
    assert any(c["id"] == "bitbucket:auth" and c["status"] == "pass" for c in checks)


@pytest.mark.parametrize("repo", ["PROJ/.", "PROJ/.."])
def test_dot_segment_repo_refused_before_request(harness: Harness, repo: str) -> None:
    config(harness)
    result = harness.run("bitbucket", "repo", "get", repo)
    assert result.code == 2
    assert harness.server.requests == []
    doc = json.dumps({"repo": repo, "source": "a", "target": "b", "title": "Test"})
    result = harness.run("bitbucket", "pr", "create", "--input", "-", stdin=doc.encode())
    assert result.code == 2
    assert harness.server.requests == []


def test_stored_basic_credential_refused_before_request(harness: Harness) -> None:
    config(harness)
    CredentialStore(harness.keyring).save(
        "bitbucket", BITBUCKET, Credential(auth="basic", username="u", secret="secret")
    )
    result = harness.run("bitbucket", "repo", "list")
    assert result.code == 3
    assert result.error["code"] == "auth_failed"
    assert harness.server.requests == []


@pytest.mark.parametrize("bad", ["missing_hunks", "missing_coordinate", "string_coordinate"])
def test_malformed_text_hunks_never_claim_complete(harness: Harness, bad: str) -> None:
    config(harness)
    diff = fixture("bitbucket_diff.json")
    file = diff["diffs"][0]
    if bad == "missing_hunks":
        file.pop("hunks")
    elif bad == "missing_coordinate":
        file["hunks"][0].pop("sourceLine")
    else:
        file["hunks"][0]["sourceLine"] = "1"
    harness.server.add("GET", DIFF, diff)
    result = harness.run("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web")
    assert result.error["code"] == "response_invalid"


def test_reviewers_over_ten_are_reported(harness: Harness) -> None:
    config(harness)
    pr = fixture("bitbucket_pull_request.json")
    pr["reviewers"] = [copy.deepcopy(pr["reviewers"][0]) for _ in range(11)]
    harness.server.add("GET", PR, pr)
    result = harness.run("bitbucket", "pr", "get", "42", "--repo", "PROJ/web")
    assert result.code == 0, result.out
    assert result.data["reviewers_total"] == 11
    assert len(result.data["reviewers"]) == 10
    assert result.data["reviewers_truncated"] is True
    assert result.data["reviewers_url"].endswith("/pull-requests/42/overview")


def test_leading_dash_diff_continuation_parses(harness: Harness) -> None:
    config(harness)
    diff = fixture("bitbucket_diff.json")
    diff["diffs"][0]["source"]["toString"] = "-old.py"
    diff["diffs"][0]["destination"]["toString"] = "-new.py"
    diff["diffs"][0]["truncated"] = True
    harness.server.add("GET", DIFF, diff)
    result = harness.run("bitbucket", "pr", "diff", "42", "--repo", "PROJ/web", "--full")
    more = shlex.split(result.data["more"])
    parsed = build_parser().parse_args(more[1:])
    assert (parsed.path, parsed.old_path) == ("-new.py", "-old.py")


@pytest.mark.parametrize(
    "readback",
    [None, {"id": 777, "text": "wrong"}, {"id": 778, "text": "Ready for review."}],
)
def test_comment_needs_scoped_readback(harness: Harness, readback: Any) -> None:
    setup_comment(harness)
    harness.server.routes[("GET", COMMENTS + "/777")] = []
    if readback is not None:
        harness.server.add("GET", COMMENTS + "/777", readback)
    result = harness.run(*COMMENT_ARGV, "--body", "Ready for review.")
    assert result.error["code"] == "outcome_unknown"
    assert "retry" not in result.error["details"]
    assert len(harness.server.writes()) == 1


def test_duplicate_refusal_with_no_retry_is_actionable(harness: Harness) -> None:
    setup_create(harness, [fixture("bitbucket_pull_request.json")])
    result = harness.run(*CREATE_ARGV)
    assert refusal(result)["retry"] is None
    assert "No corrected retry command" in result.error["fix"]


@pytest.mark.parametrize(
    "context",
    [
        ["--base-url", BITBUCKET],
        ["--config", "yojo.yaml"],
        ["--config", "yojo.yaml", "--base-url", BITBUCKET],
    ],
)
def test_continuation_and_recovery_preserve_context(harness: Harness, context: list[str]) -> None:
    config(harness)
    if context[0] == "--base-url":
        (harness.tmp_path / "yojo.yaml").unlink()
    pr = fixture("bitbucket_pull_request.json")
    pr["description"] = "x" * 2200
    harness.server.add("GET", PR, pr)
    read = harness.run("bitbucket", "pr", "get", "42", "--repo", "PROJ/web", *context)
    assert shlex.split(read.data["more"])[1 : 1 + len(context)] == context

    setup_create(harness)
    if context[0] == "--base-url":
        (harness.tmp_path / "yojo.yaml").unlink()

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    harness.server.routes[("POST", PRS)] = []
    harness.server.add("POST", PRS, handler=timeout)
    result = harness.run(*CREATE_ARGV, *context)
    assert result.error["code"] == "outcome_unknown"
    assert result.error["details"]["recovery_argv"]["list"][1 : 1 + len(context)] == context


def test_diff_and_comment_recovery_keep_explicit_url(harness: Harness) -> None:
    config(harness)
    diff = fixture("bitbucket_diff.json")
    diff["diffs"][0]["truncated"] = True
    harness.server.add("GET", DIFF, diff)
    result = harness.run(
        "bitbucket", "pr", "diff", "42", "--repo", "PROJ/web", "--base-url", BITBUCKET
    )
    assert shlex.split(result.data["more"])[1:3] == ["--base-url", BITBUCKET]

    setup_comment(harness)
    harness.server.routes[("POST", COMMENTS)] = []

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    harness.server.add("POST", COMMENTS, handler=timeout)
    result = harness.run(*COMMENT_ARGV, "--body", "Ready for review.", "--base-url", BITBUCKET)
    assert result.error["details"]["recovery_argv"]["pull_request"][1:3] == [
        "--base-url",
        BITBUCKET,
    ]


@pytest.mark.parametrize("change", ["bad_id", "source", "source_repo", "target_repo"])
def test_create_response_must_confirm_identity(harness: Harness, change: str) -> None:
    setup_create(harness)
    response = fixture("bitbucket_pull_request.json")
    if change == "bad_id":
        response["id"] = 0
    elif change == "source":
        response["fromRef"]["id"] = "refs/heads/other"
    elif change == "source_repo":
        response["fromRef"]["repository"]["slug"] = "other"
    else:
        response["toRef"]["repository"]["slug"] = "other"
    harness.server.routes[("POST", PRS)] = []
    harness.server.add("POST", PRS, response, status=201)
    result = harness.run(*CREATE_ARGV)
    assert result.error["code"] == "outcome_unknown"
    assert "retry" not in result.error["details"]
    assert "recovery_argv" in result.error["details"]


@pytest.mark.parametrize("kind", ["create", "comment"])
def test_duplicate_scan_ceiling_refuses_without_post(harness: Harness, kind: str) -> None:
    if kind == "create":
        setup_create(harness)
        endpoint, argv = PRS, CREATE_ARGV
    else:
        setup_comment(harness)
        endpoint, argv = ACTIVITIES, [*COMMENT_ARGV, "--body", "New note"]
    harness.server.routes[("GET", endpoint)] = []
    for start in range(10):
        harness.server.add("GET", endpoint, page([], start=start, next_start=start + 1))
    result = harness.run(*argv)
    checks = refusal(result)["checks"]
    assert any(c["id"] == "duplicate" and c["status"] == "unverified" for c in checks)
    assert len([r for r in harness.server.requests if r.url.path == endpoint]) == 10
    assert harness.server.writes() == []


def test_incomplete_branch_scan_refuses_without_post(harness: Harness) -> None:
    setup_create(harness)
    harness.server.routes[("GET", BRANCHES)] = []
    for start in range(10):
        harness.server.add("GET", BRANCHES, page([], start=start, next_start=start + 1))
    result = harness.run(*CREATE_ARGV)
    checks = refusal(result)["checks"]
    assert any(c["id"] == "source" and c["status"] == "unverified" for c in checks)
    assert harness.server.writes() == []


@pytest.mark.parametrize(
    "noun,endpoint,extra",
    [
        ("repo", REPOS, []),
        ("branch", BRANCHES, ["--repo", "PROJ/web"]),
        ("pr", PRS, ["--repo", "PROJ/web"]),
    ],
)
def test_list_limits(harness: Harness, noun: str, endpoint: str, extra: list[str]) -> None:
    config(harness)
    harness.server.add("GET", endpoint, page([]))
    for limit in (None, 100):
        args = ["bitbucket", noun, "list", *extra]
        if limit is not None:
            args += ["--limit", str(limit)]
        result = harness.run(*args)
        assert result.code == 0, result.out
        assert harness.server.requests[-1].url.params["limit"] == str(limit or 20)
    count = len(harness.server.requests)
    for limit in (0, 101):
        assert harness.run("bitbucket", noun, "list", *extra, "--limit", str(limit)).code == 2
    assert len(harness.server.requests) == count
