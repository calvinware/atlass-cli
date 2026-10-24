from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from atlass_cli.features import jql_quote, rank, tokenize
from tests.conftest import JIRA, Harness, config_text, fixture, jira_path

ISSUETYPES = jira_path("/rest/api/2/issue/createmeta/EXAMPLE/issuetypes")
STORY_FIELDS = jira_path("/rest/api/2/issue/createmeta/EXAMPLE/issuetypes/10001")
FEATURE_FIELDS = jira_path("/rest/api/2/issue/createmeta/EXAMPLE/issuetypes/10000")
CREATE = jira_path("/rest/api/2/issue")
FEATURE_ISSUE = jira_path("/rest/api/2/issue/EXAMPLE-10")
SEARCH = jira_path("/rest/api/2/search")

STORY = {
    "project": "EXAMPLE",
    "summary": "Add SSO button",
    "acceptance_criteria": "Given a user...\nThen they are signed in.",
    "feature_key": "EXAMPLE-10",
}


def setup_create(
    harness: Harness, *, policy: str = "human_confirmed", story_fields: Any = None
) -> None:
    harness.write_config(config_text(policy=policy))
    harness.store()
    harness.server.add("GET", ISSUETYPES, fixture("createmeta_issuetypes.json"))
    harness.server.add("GET", STORY_FIELDS, story_fields or fixture("createmeta_story_fields.json"))
    harness.server.add("GET", FEATURE_FIELDS, fixture("createmeta_feature_fields.json"))
    harness.server.add("GET", FEATURE_ISSUE, fixture("issue_feature.json"))
    harness.server.add(
        "POST", CREATE, {"id": "30001", "key": "EXAMPLE-42", "self": "x"}, status=201
    )


def create_story(harness: Harness, doc: dict[str, Any]) -> Any:
    return harness.run("jira", "story", "create", "--input", "-", stdin=json.dumps(doc).encode())


def checks(error: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in error["details"]["checks"]}


# --------------------------------------------------------------------------- story create


def test_story_create_success(harness: Harness) -> None:
    setup_create(harness)
    result = create_story(harness, {**STORY, "description": "desc"})
    assert result.code == 0
    assert result.data["key"] == "EXAMPLE-42"
    assert result.data["url"] == f"{JIRA}/browse/EXAMPLE-42"
    (post,) = harness.server.writes()
    assert json.loads(post.content) == {
        "fields": {
            "project": {"key": "EXAMPLE"},
            "issuetype": {"id": "10001"},
            "summary": "Add SSO button",
            "customfield_10100": STORY["acceptance_criteria"],
            "customfield_10200": "EXAMPLE-10",
            "description": "desc",
        }
    }


def test_story_create_missing_acceptance_criteria(harness: Harness) -> None:
    setup_create(harness)
    doc = {k: v for k, v in STORY.items() if k != "acceptance_criteria"}
    result = create_story(harness, doc)
    assert result.code == 4
    check = checks(result.error)["acceptance_criteria"]
    assert check["status"] == "fail"
    assert "never generates" in check["fix"]
    assert harness.server.writes() == []


def test_story_create_blank_acceptance_criteria(harness: Harness) -> None:
    setup_create(harness)
    result = create_story(harness, {**STORY, "acceptance_criteria": "   "})
    assert checks(result.error)["acceptance_criteria"]["status"] == "fail"


def test_story_create_missing_feature(harness: Harness) -> None:
    setup_create(harness)
    doc = {k: v for k, v in STORY.items() if k != "feature_key"}
    result = create_story(harness, doc)
    assert result.code == 4
    check = checks(result.error)["feature_key"]
    assert "atl jira feature find" in check["fix"]
    assert "never chooses" in check["fix"]
    assert harness.server.writes() == []


def test_story_create_feature_wrong_type(harness: Harness) -> None:
    setup_create(harness)
    harness.server.routes[("GET", FEATURE_ISSUE)] = []
    harness.server.add("GET", FEATURE_ISSUE, fixture("issue_todo.json"))
    result = create_story(harness, STORY)
    assert "not a Feature" in checks(result.error)["feature_key"]["message"]


def test_story_create_feature_not_found(harness: Harness) -> None:
    setup_create(harness)
    harness.server.routes[("GET", FEATURE_ISSUE)] = []
    harness.server.add("GET", FEATURE_ISSUE, {"errorMessages": ["nope"]}, status=404)
    result = create_story(harness, STORY)
    assert checks(result.error)["feature_key"]["status"] == "fail"


def test_story_create_field_not_on_screen(harness: Harness) -> None:
    meta = fixture("createmeta_story_fields.json")
    meta["values"] = [v for v in meta["values"] if v["fieldId"] != "customfield_10100"]
    meta["total"] = len(meta["values"])
    setup_create(harness, story_fields=meta)
    result = create_story(harness, STORY)
    assert result.code == 4
    assert "create screen" in checks(result.error)["field:acceptance_criteria"]["message"]
    assert harness.server.writes() == []


def test_story_create_unmapped_live_required_field(harness: Harness) -> None:
    meta = fixture("createmeta_story_fields.json")
    meta["values"].append(
        {
            "fieldId": "customfield_10900",
            "name": "Team",
            "required": True,
            "hasDefaultValue": False,
            "schema": {"type": "string"},
            "operations": ["set"],
        }
    )
    meta["total"] = len(meta["values"])
    setup_create(harness, story_fields=meta)
    result = create_story(harness, STORY)
    check = checks(result.error)["create_field:customfield_10900"]
    assert check["status"] == "fail"
    assert "Team" in check["fix"]


def test_story_create_ambiguous_field_name(harness: Harness) -> None:
    meta = fixture("createmeta_story_fields.json")
    dup = dict(meta["values"][-2])
    dup["fieldId"] = "customfield_19999"
    meta["values"].append(dup)
    meta["total"] = len(meta["values"])
    setup_create(harness, story_fields=meta)
    result = create_story(harness, STORY)
    check = checks(result.error)["field:acceptance_criteria"]
    assert "customfield_19999" in check["fix"]
    assert "explicit id" in check["fix"]


def test_story_create_with_explicit_id(harness: Harness) -> None:
    setup_create(harness)
    harness.write_config(
        config_text().replace(
            "{jira_name: Acceptance Criteria, encoding: text}",
            "{id: customfield_10100, encoding: text}",
        )
    )
    assert create_story(harness, STORY).code == 0


def test_story_create_encoding_mismatch(harness: Harness) -> None:
    meta = fixture("createmeta_story_fields.json")
    for value in meta["values"]:
        if value["fieldId"] == "customfield_10100":
            value["schema"] = {"type": "number"}
    setup_create(harness, story_fields=meta)
    result = create_story(harness, STORY)
    assert "encoding" in checks(result.error)["field:acceptance_criteria"]["message"]


def test_story_create_unknown_input_field(harness: Harness) -> None:
    setup_create(harness)
    result = create_story(harness, {**STORY, "fields": {"nope": "x"}})
    assert result.code == 2
    assert result.error["code"] == "input_invalid"
    assert harness.server.requests == []


def test_story_create_reserved_field_in_fields(harness: Harness) -> None:
    setup_create(harness)
    result = create_story(harness, {**STORY, "fields": {"acceptance_criteria": "x"}})
    assert result.error["code"] == "input_invalid"


def test_createmeta_pagination(harness: Harness) -> None:
    meta = fixture("createmeta_story_fields.json")
    first = {**meta, "values": meta["values"][:4], "isLast": False, "maxResults": 4}
    second = {**meta, "values": meta["values"][4:], "isLast": True, "startAt": 4}
    setup_create(harness)
    harness.server.routes[("GET", STORY_FIELDS)] = []
    harness.server.add("GET", STORY_FIELDS, first)
    harness.server.add("GET", STORY_FIELDS, second)
    assert create_story(harness, STORY).code == 0
    starts = [
        r.url.params["startAt"] for r in harness.server.requests if r.url.path == STORY_FIELDS
    ]
    assert starts == ["0", "4"]


def test_createmeta_truncated_fails_closed(harness: Harness) -> None:
    meta = fixture("createmeta_story_fields.json")
    setup_create(harness)
    harness.server.routes[("GET", STORY_FIELDS)] = []
    harness.server.add("GET", STORY_FIELDS, {**meta, "values": meta["values"][:4], "isLast": False})
    harness.server.add("GET", STORY_FIELDS, {**meta, "values": [], "isLast": False, "startAt": 4})
    result = create_story(harness, STORY)
    assert result.code == 6
    assert result.error["code"] == "metadata_incomplete"
    assert harness.server.writes() == []


@pytest.mark.parametrize("damage", ["short_final_page", "malformed_field", "duplicate_field"])
def test_createmeta_inconsistent_final_page_fails_closed(harness: Harness, damage: str) -> None:
    meta = fixture("createmeta_story_fields.json")
    if damage == "short_final_page":
        meta["values"] = meta["values"][:-1]
    elif damage == "malformed_field":
        meta["values"][-1] = {"name": "Parent Link"}
    else:
        meta["values"][-1]["fieldId"] = meta["values"][-2]["fieldId"]
    setup_create(harness, story_fields=meta)
    result = create_story(harness, STORY)
    assert result.code == 6
    assert result.error["code"] == "metadata_incomplete"
    assert harness.server.writes() == []


def test_story_create_remote_400_is_sanitized(harness: Harness) -> None:
    setup_create(harness)
    harness.server.routes[("POST", CREATE)] = []
    harness.server.add(
        "POST", CREATE, {"errors": {"customfield_10200": "Parent invalid"}}, status=400
    )
    result = create_story(harness, STORY)
    assert result.code == 5
    assert result.error["details"]["fields"] == {"customfield_10200": "Parent invalid"}


# --------------------------------------------------------------------------- feature create


def create_feature(harness: Harness, doc: dict[str, Any]) -> Any:
    return harness.run("jira", "feature", "create", "--input", "-", stdin=json.dumps(doc).encode())


def test_feature_create_requires_attestation(harness: Harness) -> None:
    setup_create(harness)
    result = create_feature(harness, {"project": "EXAMPLE", "summary": "SSO"})
    assert result.code == 4
    assert result.error["code"] == "approval_required"
    assert "not verify the identity" in result.error["fix"]
    assert harness.server.requests == []


def test_feature_create_with_attestation(harness: Harness) -> None:
    setup_create(harness)
    doc = {
        "project": "EXAMPLE",
        "summary": "SSO",
        "human_confirmation": {"confirmed_by": "Pat Lee", "statement": "Approved in standup"},
    }
    result = create_feature(harness, doc)
    assert result.code == 0
    assert result.data["attestation"]["identity_verified"] is False
    assert result.data["policy"] == "human_confirmed"
    (post,) = harness.server.writes()
    assert json.loads(post.content)["fields"]["issuetype"] == {"id": "10000"}


def test_feature_create_automatic_policy(harness: Harness) -> None:
    setup_create(harness, policy="automatic")
    result = create_feature(harness, {"project": "EXAMPLE", "summary": "SSO"})
    assert result.code == 0
    assert result.data["attestation"] is None


# --------------------------------------------------------------------------- feature find


def setup_find(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    harness.server.add("GET", SEARCH, fixture("search_features.json"))
    harness.server.add("GET", jira_path("/rest/api/2/issue/EXAMPLE-1"), fixture("issue_todo.json"))


def test_feature_find_text_ranks_and_never_selects(harness: Harness) -> None:
    setup_find(harness)
    result = harness.run("jira", "feature", "find", "--text", 'SSO "login" sign-on')
    assert result.code == 0
    data = result.data
    assert data["selection"] == "caller"
    assert [c["key"] for c in data["candidates"]] == ["EXAMPLE-10", "EXAMPLE-12"]
    assert data["candidates"][0]["score"] > data["candidates"][1]["score"]
    jql = harness.server.requests[0].url.params["jql"]
    assert '(project = "EXAMPLE" AND issuetype = "Feature")' in jql
    assert 'summary ~ "login"' in jql
    assert harness.server.writes() == []


def test_feature_find_key_like_text_is_text(harness: Harness) -> None:
    setup_find(harness)
    data = harness.run("jira", "feature", "find", "--text", "EXAMPLE-11 dashboards").data
    top = data["candidates"][0]
    assert top["key"] == "EXAMPLE-11"
    assert "exact key mentioned in the query" in top["reasons"]
    jql = harness.server.requests[0].url.params["jql"]
    assert 'key in ("EXAMPLE-11")' in jql


def test_feature_find_by_issue(harness: Harness) -> None:
    setup_find(harness)
    data = harness.run("jira", "feature", "find", "--issue", "EXAMPLE-1").data
    assert data["mode"] == "issue"
    assert data["candidates"][0]["key"] == "EXAMPLE-10"
    assert any("same project" in r for r in data["candidates"][0]["reasons"])


def test_feature_find_requires_meaningful_text(harness: Harness) -> None:
    setup_find(harness)
    result = harness.run("jira", "feature", "find", "--text", "a to of")
    assert result.code == 2
    assert harness.server.requests == []


def test_jql_quote_escapes() -> None:
    assert jql_quote('a "b" \\ c') == '"a \\"b\\" \\\\ c"'
    assert jql_quote("x\ny") == '"xy"'


def test_rank_is_stable_with_key_tiebreak() -> None:
    issues = [
        {"key": "EXAMPLE-100", "fields": {"summary": "login", "project": {"key": "EXAMPLE"}}},
        {"key": "EXAMPLE-9", "fields": {"summary": "login", "project": {"key": "EXAMPLE"}}},
        {"key": "ALPHA-50", "fields": {"summary": "login", "project": {"key": "ALPHA"}}},
    ]
    ranked = rank(issues, tokenize("login"), set(), None, None, 10)
    assert [c.key for c in ranked] == ["ALPHA-50", "EXAMPLE-9", "EXAMPLE-100"]
    assert [c.key for c in rank(issues, ["login"], set(), None, None, 2)] == [
        "ALPHA-50",
        "EXAMPLE-9",
    ]


# --------------------------------------------------------------------------- attachments


ATTACH = jira_path("/rest/api/2/issue/EXAMPLE-1/attachments")


def setup_attach(harness: Harness, limit: int = 10) -> None:
    harness.write_config(config_text(max_bytes=limit))
    harness.store()


def _attachment_ok(request: httpx.Request) -> httpx.Response:
    request.read()
    return httpx.Response(200, json=[{"id": "500", "filename": "f.txt", "size": 10}])


def test_attachment_absent_file(harness: Harness) -> None:
    setup_attach(harness)
    result = harness.run("jira", "attachment", "add", "EXAMPLE-1", "--file", "missing.txt")
    assert result.code == 2
    assert result.error["code"] == "file_invalid"
    assert harness.server.requests == []


def test_attachment_directory_rejected(harness: Harness) -> None:
    setup_attach(harness)
    (harness.tmp_path / "dir").mkdir()
    assert harness.run("jira", "attachment", "add", "EXAMPLE-1", "--file", "dir").code == 2


def test_attachment_over_limit(harness: Harness) -> None:
    setup_attach(harness)
    (harness.tmp_path / "f.txt").write_bytes(b"x" * 11)
    result = harness.run("jira", "attachment", "add", "EXAMPLE-1", "--file", "f.txt")
    assert result.code == 2
    assert result.error["code"] == "attachment_too_large"
    assert result.error["details"] == {"size": 11, "limit": 10}
    assert harness.server.requests == []


def test_attachment_exactly_at_limit(harness: Harness) -> None:
    setup_attach(harness)
    (harness.tmp_path / "f.txt").write_bytes(b"x" * 10)
    harness.server.add("POST", ATTACH, handler=_attachment_ok)
    result = harness.run("jira", "attachment", "add", "EXAMPLE-1", "--file", "f.txt")
    assert result.code == 0
    assert result.data["attachments"][0]["id"] == "500"
    (request,) = harness.server.requests
    assert request.headers["x-atlassian-token"] == "no-check"
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert int(request.headers["content-length"]) == len(request.content)
    assert b'filename="f.txt"' in request.content
    assert b"\r\n\r\nxxxxxxxxxx\r\n--" in request.content


def test_attachment_growing_file_never_exceeds_declared_size(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_attach(harness)
    path = harness.tmp_path / "f.txt"
    path.write_bytes(b"x" * 5)
    real_stat = Path.stat
    declared = real_stat(path)

    def stat_then_grow(self: Path, **kwargs: Any) -> os.stat_result:
        if self == path:
            with path.open("ab") as handle:
                handle.write(b"y" * 100)
            return declared
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_then_grow)
    harness.server.add("POST", ATTACH, handler=_attachment_ok)
    result = harness.run("jira", "attachment", "add", "EXAMPLE-1", "--file", "f.txt")
    assert result.code == 2
    assert result.error["code"] == "file_invalid"
    assert result.error["details"]["dispatched"] == "aborted"
    assert result.error["details"]["declared_size"] == 5
    # The mock transport buffers the body before routing; the abort happens while buffering.
    assert harness.server.requests == []


def test_attachment_server_limit(harness: Harness) -> None:
    setup_attach(harness)
    (harness.tmp_path / "f.txt").write_bytes(b"x")
    harness.server.add("POST", ATTACH, status=413, body={"errorMessages": ["too big"]})
    result = harness.run("jira", "attachment", "add", "EXAMPLE-1", "--file", "f.txt")
    assert result.code == 5
    assert result.error["code"] == "remote_limit_exceeded"
    assert len(harness.server.requests) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="quotes are invalid in Windows names")
def test_attachment_filename_header_is_escaped(harness: Harness) -> None:
    setup_attach(harness)
    (harness.tmp_path / 'we"ird.txt').write_bytes(b"x")
    harness.server.add("POST", ATTACH, handler=_attachment_ok)
    assert harness.run("jira", "attachment", "add", "EXAMPLE-1", "--file", 'we"ird.txt').code == 0
    assert b'filename="we_ird.txt"' in harness.server.requests[0].content


# --------------------------------------------------------------------------- comments / search


def test_comment_from_stdin(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    harness.server.add(
        "POST", jira_path("/rest/api/2/issue/EXAMPLE-1/comment"), {"id": "777"}, status=201
    )
    result = harness.run(
        "jira", "comment", "add", "EXAMPLE-1", "--body-file", "-", stdin="Hé\n".encode()
    )
    assert result.data == {"key": "EXAMPLE-1", "comment_id": "777"}
    assert json.loads(harness.server.requests[0].content) == {"body": "Hé\n"}


def test_comment_from_file_and_empty(harness: Harness, tmp_path: Path) -> None:
    harness.write_config()
    harness.store()
    (harness.tmp_path / "c.txt").write_text("   \n")
    result = harness.run("jira", "comment", "add", "EXAMPLE-1", "--body-file", "c.txt")
    assert result.error["code"] == "input_invalid"
    assert harness.server.requests == []


def test_search_shape_and_pagination(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    body = {
        "startAt": 0,
        "maxResults": 2,
        "total": 3,
        "issues": [
            {
                "id": "1",
                "key": "EXAMPLE-1",
                "fields": {
                    "summary": "a",
                    "status": {"name": "To Do"},
                    "issuetype": {"name": "Story"},
                    "project": {"key": "EXAMPLE"},
                    "assignee": None,
                    "updated": "2026-10-01",
                },
            },
            {"id": "2", "key": "EXAMPLE-2", "fields": {}},
        ],
    }
    harness.server.add("GET", SEARCH, body)
    result = harness.run("jira", "search", "--jql", "project = EXAMPLE", "--limit", "2")
    assert result.data["next_start"] == 2
    assert result.data["issues"][1] == {
        "key": "EXAMPLE-2",
        "id": "2",
        "summary": None,
        "status": None,
        "issue_type": None,
        "project": None,
        "assignee": None,
        "updated": None,
    }
    params = harness.server.requests[0].url.params
    assert params["maxResults"] == "2"
    assert params["startAt"] == "0"


def test_search_invalid_jql_is_remote_validation(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    harness.server.add("GET", SEARCH, {"errorMessages": ["Error in the JQL Query"]}, status=400)
    result = harness.run("jira", "search", "--jql", "project = = X")
    assert result.code == 5
    assert result.error["details"]["messages"] == ["Error in the JQL Query"]


# --------------------------------------------------------------------------- config check


def setup_config_check(harness: Harness, *, sample_found: bool = True) -> None:
    setup_create(harness)
    issue_search = {"issues": [{"key": "EXAMPLE-1"}]} if sample_found else {"issues": []}
    harness.server.add("GET", SEARCH, issue_search)
    harness.server.add("GET", jira_path("/rest/api/2/issue/EXAMPLE-1"), fixture("issue_todo.json"))
    harness.server.add(
        "GET",
        jira_path("/rest/api/2/issue/EXAMPLE-1/transitions"),
        fixture("transitions_todo.json"),
    )


def test_config_check_unverified_without_samples(harness: Harness) -> None:
    setup_config_check(harness, sample_found=False)
    result = harness.run("config", "check")
    assert result.code == 4
    assert result.error["code"] == "config_unverified"
    unverified = [c for c in result.error["details"]["checks"] if c["status"] == "unverified"]
    assert {c["id"] for c in unverified} == {
        "EXAMPLE:transition:in_progress:To Do",
        "EXAMPLE:transition:done:In Progress",
    }
    assert "--issue" in unverified[0]["fix"]
    assert harness.server.writes() == []


def test_config_check_drift_on_missing_issue_type(harness: Harness) -> None:
    setup_config_check(harness)
    types = fixture("createmeta_issuetypes.json")
    types["values"] = [t for t in types["values"] if t["name"] != "Feature"]
    types["total"] = len(types["values"])
    harness.server.routes[("GET", ISSUETYPES)] = []
    harness.server.add("GET", ISSUETYPES, types)
    result = harness.run("config", "check", "--issue", "EXAMPLE-1")
    assert result.code == 4
    assert result.error["code"] == "config_drift"
    drift = {c["id"] for c in result.error["details"]["checks"] if c["status"] == "drift"}
    assert "EXAMPLE:issue_type:Feature" in drift


def test_config_check_probes_transitions_with_sample(harness: Harness) -> None:
    setup_config_check(harness)
    result = harness.run("config", "check", "--issue", "EXAMPLE-1")
    statuses = {c["id"]: c["status"] for c in result.error["details"]["checks"]}
    assert statuses["EXAMPLE:transition:in_progress:To Do:transition_available"] == "pass"
    assert statuses["EXAMPLE:transition:in_progress:To Do:auto_set:work_started"] == "pass"
    # The sample search returns EXAMPLE-1 (To Do) for every status, so `done` cannot be offered.
    assert statuses["EXAMPLE:transition:done:In Progress:transition_available"] == "drift"


def test_config_check_truncated_metadata_is_unverified(harness: Harness) -> None:
    setup_config_check(harness)
    meta = fixture("createmeta_story_fields.json")
    harness.server.routes[("GET", STORY_FIELDS)] = []
    harness.server.add("GET", STORY_FIELDS, {**meta, "isLast": False})
    harness.server.add("GET", STORY_FIELDS, {**meta, "values": [], "isLast": False})
    result = harness.run("config", "check", "--issue", "EXAMPLE-1")
    statuses = {c["id"]: c["status"] for c in result.error["details"]["checks"]}
    assert statuses["EXAMPLE:Story:create_fields"] == "unverified"


def test_config_check_pass(harness: Harness) -> None:
    harness.write_config(
        config_text()
        .replace(
            "          require:\n            - kind: attachment_filename\n"
            '              pattern: "{key}_TestResults_*"\n',
            "",
        )
        .replace("        done:\n          from: [In Progress]\n          to: Done\n", "")
    )
    harness.store()
    harness.server.add("GET", ISSUETYPES, fixture("createmeta_issuetypes.json"))
    harness.server.add("GET", STORY_FIELDS, fixture("createmeta_story_fields.json"))
    harness.server.add("GET", FEATURE_FIELDS, fixture("createmeta_feature_fields.json"))
    harness.server.add("GET", SEARCH, {"issues": [{"key": "EXAMPLE-1"}]})
    harness.server.add("GET", jira_path("/rest/api/2/issue/EXAMPLE-1"), fixture("issue_todo.json"))
    harness.server.add(
        "GET",
        jira_path("/rest/api/2/issue/EXAMPLE-1/transitions"),
        fixture("transitions_todo.json"),
    )
    result = harness.run("config", "check")
    assert result.code == 0, result.out
    assert result.data["status"] == "pass"


@pytest.mark.parametrize("encoding_schema", [{"type": "number"}])
def test_config_check_encoding_drift(harness: Harness, encoding_schema: dict[str, Any]) -> None:
    meta = fixture("createmeta_story_fields.json")
    for value in meta["values"]:
        if value["fieldId"] == "customfield_10200":
            value["schema"] = encoding_schema
    setup_config_check(harness)
    harness.server.routes[("GET", STORY_FIELDS)] = []
    harness.server.add("GET", STORY_FIELDS, meta)
    result = harness.run("config", "check", "--issue", "EXAMPLE-1")
    statuses = {c["id"]: c["status"] for c in result.error["details"]["checks"]}
    assert statuses["EXAMPLE:Story:field:feature_link"] == "drift"
