from __future__ import annotations

import json
import os
import shlex
import signal
import stat
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import (
    JIRA,
    JIRA_TOKEN,
    FakeServer,
    Harness,
    config_text,
    fixture,
    jira_path,
    run_argv,
)
from yojo import jira as jira_module
from yojo.auth import Credential
from yojo.config import parse_config_file
from yojo.contracts import YojoError
from yojo.jira import attachment_add
from yojo.transport import ServiceClient, jql_quote

ISSUETYPES = jira_path("/rest/api/2/issue/createmeta/EXAMPLE/issuetypes")
BUG_FIELDS = jira_path("/rest/api/2/issue/createmeta/EXAMPLE/issuetypes/10002")
CREATE = jira_path("/rest/api/2/issue")
ISSUE = jira_path("/rest/api/2/issue/EXAMPLE-1")
ISSUE_2 = jira_path("/rest/api/2/issue/EXAMPLE-2")
NEW_ISSUE = jira_path("/rest/api/2/issue/EXAMPLE-42")
EDITMETA = jira_path("/rest/api/2/issue/EXAMPLE-1/editmeta")
FIELDS = jira_path("/rest/api/2/field")
LINK_TYPES = jira_path("/rest/api/2/issueLinkType")
LINK = jira_path("/rest/api/2/issueLink")
PERMISSIONS = jira_path("/rest/api/2/mypermissions")
ATTACH_META = jira_path("/rest/api/2/attachment/meta")
COMMENT = jira_path("/rest/api/2/issue/EXAMPLE-1/comment")
USERS = jira_path("/rest/api/2/user/search")
SEARCH = jira_path("/rest/api/2/search")

BUG = [
    "jira",
    "issue",
    "create",
    "EXAMPLE",
    "--type",
    "Bug",
    "--summary",
    "Login button misaligned",
    "--set",
    "Severity=major",
    "--set",
    "Environment=Firefox on Linux",
]


def permissions(**granted: bool) -> dict[str, Any]:
    return {"permissions": {k: {"key": k, "havePermission": v} for k, v in granted.items()}}


def checks(error: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in error["details"]["checks"]}


def body_of(request: httpx.Request) -> Any:
    return json.loads(request.content)


def issue_reply(key: str = "EXAMPLE-1", **fields: Any) -> dict[str, Any]:
    issue: dict[str, Any] = fixture("issue_todo.json")
    issue["key"] = key
    issue["fields"].update(fields)
    return issue


# --------------------------------------------------------------------------- issue create


def setup_create(harness: Harness, *, bug_fields: Any = None, config: str | None = None) -> None:
    harness.write_config(config or config_text())
    harness.store()
    harness.server.add("GET", ISSUETYPES, fixture("createmeta_issuetypes.json"))
    harness.server.add("GET", BUG_FIELDS, bug_fields or fixture("createmeta_bug_fields.json"))
    harness.server.add(
        "POST", CREATE, {"id": "30001", "key": "EXAMPLE-42", "self": "x"}, status=201
    )
    harness.server.add("GET", NEW_ISSUE, issue_reply("EXAMPLE-42"))


def test_create_bug_in_one_call(harness: Harness) -> None:
    setup_create(harness)
    result = harness.run(
        *BUG, "--set", "Priority=High", "--set", "labels=ui, login", "--set", "Story Points=3"
    )
    assert result.code == 0, result.out
    assert result.data == {
        "key": "EXAMPLE-42",
        "url": JIRA + "/browse/EXAMPLE-42",
        "status": "To Do",
        "issue_type": "Bug",
        "project": "EXAMPLE",
        "changed": [
            {"field": "Summary", "id": "summary"},
            {"field": "Severity", "id": "customfield_10500"},
            {"field": "Environment", "id": "customfield_10600"},
            {"field": "Priority", "id": "priority"},
            {"field": "Labels", "id": "labels"},
            {"field": "Story Points", "id": "customfield_10002"},
        ],
    }
    (post,) = harness.server.writes()
    assert body_of(post) == {
        "fields": {
            "customfield_10500": {"id": "21"},
            "customfield_10600": "Firefox on Linux",
            "priority": {"id": "2"},
            "labels": ["ui", "login"],
            "customfield_10002": 3,
            "project": {"key": "EXAMPLE"},
            "issuetype": {"id": "10002"},
            "summary": "Login button misaligned",
        }
    }


def test_create_with_input_document(harness: Harness) -> None:
    setup_create(harness)
    doc = {
        "project": "EXAMPLE",
        "type": "bug",
        "summary": "From input",
        "description": "Steps:\n1. open",
        "fields": {"Severity": "Minor", "Environment": "CI", "labels": ["a", "b"]},
    }
    result = harness.run("jira", "issue", "create", "--input", "-", stdin=json.dumps(doc).encode())
    assert result.code == 0, result.out
    fields = body_of(harness.server.writes()[0])["fields"]
    assert fields["description"] == "Steps:\n1. open"
    assert fields["labels"] == ["a", "b"]
    assert fields["customfield_10500"] == {"id": "22"}


def test_create_boolean_field_from_input_document(harness: Harness) -> None:
    meta = fixture("createmeta_bug_fields.json")
    meta["values"].append(
        {
            "fieldId": "customfield_20000",
            "name": "Customer visible",
            "required": False,
            "hasDefaultValue": False,
            "operations": ["set"],
            "schema": {"type": "boolean"},
        }
    )
    meta["total"] += 1
    setup_create(harness, bug_fields=meta)
    doc = {
        "project": "EXAMPLE",
        "type": "Bug",
        "summary": "Boolean field",
        "fields": {
            "Severity": "Major",
            "Environment": "CI",
            "Customer visible": False,
        },
    }
    result = harness.run("jira", "issue", "create", "--input", "-", stdin=json.dumps(doc).encode())
    assert result.code == 0, result.out
    assert body_of(harness.server.writes()[0])["fields"]["customfield_20000"] is False


def test_create_reports_every_missing_field_at_once(harness: Harness) -> None:
    setup_create(harness)
    argv = ["jira", "issue", "create", "EXAMPLE", "--type", "Bug", "--summary", "x"]
    result = harness.run(*argv)
    assert result.code == 4
    error = result.error
    assert error["code"] == "requirements_unmet"
    assert error["details"]["nothing_sent"] is True
    found = checks(error)
    assert set(found) == {"required:customfield_10500", "required:customfield_10600"}
    assert found["required:customfield_10500"]["details"]["allowed"] == [
        "Critical",
        "Major",
        "Minor",
    ]
    assert error["details"]["retry_argv"] == [
        "yojo",
        *argv,
        "--set",
        "Severity=<value>",
        "--set",
        "Environment=<value>",
    ]
    assert shlex.split(error["details"]["retry"]) == error["details"]["retry_argv"]
    assert harness.server.writes() == []


def test_create_missing_type_and_summary_are_reported_together(harness: Harness) -> None:
    setup_create(harness)
    result = harness.run("jira", "issue", "create", "EXAMPLE")
    found = checks(result.error)
    assert found["summary"]["status"] == "fail"
    assert found["issue_type"]["details"]["allowed"] == ["Story", "Task", "Bug"]
    retry = result.error["details"]["retry_argv"]
    assert retry[-4:] == ["--summary", "<summary>", "--type", "<Story|Task|Bug>"]
    assert harness.server.writes() == []


def test_create_suggests_a_close_type_and_field(harness: Harness) -> None:
    setup_create(harness)
    argv = [*BUG[:5], "Bugg", *BUG[6:]]
    argv[argv.index("Severity=major")] = "Severty=major"
    result = harness.run(*argv)
    assert result.code == 4
    found = checks(result.error)
    assert found["issue_type"]["details"]["suggestion"] == "Bug"
    assert "Did you mean 'Bug'?" in found["issue_type"]["message"]
    # Field names cannot be checked until the type is known.
    assert found["field:Severty"]["status"] == "unverified"
    fixed = result.error["details"]["retry_argv"]
    assert fixed[fixed.index("--type") + 1] == "Bug"

    retry = run_argv(fixed)[1:]
    assert retry[retry.index("--type") + 1] == "Bug"
    result = harness.run(*retry)
    found = checks(result.error)
    assert found["field:Severty"]["details"]["suggestion"] == "Severity"
    assert "Did you mean 'Severity'?" in found["field:Severty"]["message"]
    retry = result.error["details"]["retry_argv"]
    assert "Severity=major" in retry
    assert "Severity=<value>" not in retry and "Severty=major" not in retry
    assert harness.server.writes() == []


def test_create_ambiguous_field_lists_candidates(harness: Harness) -> None:
    setup_create(harness)
    result = harness.run(*BUG, "--set", "component OWNER=me")
    assert result.code == 4
    check = checks(result.error)["field:component OWNER"]
    assert check["details"]["kind"] == "ambiguous"
    assert sorted(check["details"]["candidates"]) == ["customfield_10700", "customfield_10701"]
    assert "Component Owner" in check["message"] and "Component owner" in check["message"]
    assert harness.server.writes() == []


def test_create_exact_case_wins_among_case_insensitive_matches(harness: Harness) -> None:
    setup_create(harness)
    result = harness.run(*BUG, "--set", "Component Owner=me")
    assert result.code == 0, result.out
    assert body_of(harness.server.writes()[0])["fields"]["customfield_10700"] == "me"


def test_create_raw_field_id_and_alias(harness: Harness) -> None:
    config = config_text().replace(
        "    review_flag: {jira_name: Review Flag}\n",
        "    review_flag: {jira_name: Review Flag}\n    points: {jira_name: Story Points}\n",
    )
    setup_create(harness, config=config)
    result = harness.run(*BUG, "--set", "points=5", "--set", "CUSTOMFIELD_10700=x")
    assert result.code == 0, result.out
    fields = body_of(harness.server.writes()[0])["fields"]
    assert fields["customfield_10002"] == 5
    assert fields["customfield_10700"] == "x"


def test_create_invalid_option_lists_allowed_values(harness: Harness) -> None:
    setup_create(harness)
    argv = list(BUG)
    argv[argv.index("Severity=major")] = "Severity=Huge"
    result = harness.run(*argv)
    check = checks(result.error)["field:Severity"]
    assert "'Critical', 'Major', 'Minor'" in check["fix"]
    assert "Severity=<value>" in result.error["details"]["retry_argv"]
    assert harness.server.writes() == []


def test_create_refuses_fields_with_their_own_flags(harness: Harness) -> None:
    setup_create(harness)
    result = harness.run(*BUG, "--set", "Summary=other")
    check = checks(result.error)["field:Summary"]
    assert check["status"] == "fail"
    assert "--set" in check["fix"]
    assert "Summary=other" not in result.error["details"]["retry_argv"]
    assert harness.server.writes() == []


def test_create_field_not_on_screen(harness: Harness) -> None:
    setup_create(harness)
    result = harness.run(*BUG, "--set", "Review Flag=Yes")
    check = checks(result.error)["field:Review Flag"]
    assert check["status"] == "fail"
    assert "Review Flag=Yes" not in result.error["details"]["retry_argv"]
    assert harness.server.writes() == []


def test_create_enforces_configured_required_fields(harness: Harness) -> None:
    config = config_text().replace(
        "      transitions:\n",
        "      create:\n        bug:\n          required: [Story Points]\n      transitions:\n",
        1,
    )
    setup_create(harness, config=config)
    result = harness.run(*BUG)
    check = checks(result.error)["rule:Story Points"]
    assert "yojo.yaml requires Story Points" in check["message"]
    assert "Story Points=<value>" in result.error["details"]["retry_argv"]
    assert harness.server.writes() == []
    assert harness.run(*BUG, "--set", "Story Points=1").code == 0


def test_create_user_field_resolves_one_person(harness: Harness) -> None:
    setup_create(harness)
    harness.server.add(
        "GET", USERS, [{"name": "builder1", "key": "u1", "displayName": "Example Builder"}]
    )
    assert harness.run(*BUG, "--set", "Assignee=example builder").code == 0
    assert body_of(harness.server.writes()[0])["fields"]["assignee"] == {"name": "builder1"}


def test_create_user_field_never_guesses(harness: Harness) -> None:
    setup_create(harness)
    harness.server.add(
        "GET",
        USERS,
        [
            {"name": "builder1", "displayName": "Example Builder"},
            {"name": "builder2", "displayName": "Example Builder"},
        ],
    )
    result = harness.run(*BUG, "--set", "Assignee=Example Builder")
    check = checks(result.error)["field:Assignee"]
    assert check["status"] == "fail"
    assert "builder1" in check["fix"] and "builder2" in check["fix"]
    assert harness.server.writes() == []


def test_create_works_without_config(harness: Harness) -> None:
    setup_create(harness)
    (harness.tmp_path / "yojo.yaml").unlink()
    result = harness.run("--base-url", JIRA, *BUG)
    assert result.code == 0, result.out


def test_createmeta_pagination(harness: Harness) -> None:
    meta = fixture("createmeta_bug_fields.json")
    first = {**meta, "values": meta["values"][:4], "isLast": False, "maxResults": 4}
    second = {**meta, "values": meta["values"][4:], "isLast": True, "startAt": 4}
    setup_create(harness)
    harness.server.routes[("GET", BUG_FIELDS)] = []
    harness.server.add("GET", BUG_FIELDS, first)
    harness.server.add("GET", BUG_FIELDS, second)
    assert harness.run(*BUG).code == 0
    starts = [r.url.params["startAt"] for r in harness.server.requests if r.url.path == BUG_FIELDS]
    assert starts == ["0", "4"]


def test_createmeta_truncated_fails_closed(harness: Harness) -> None:
    meta = fixture("createmeta_bug_fields.json")
    setup_create(harness)
    harness.server.routes[("GET", BUG_FIELDS)] = []
    harness.server.add("GET", BUG_FIELDS, {**meta, "values": meta["values"][:4], "isLast": False})
    harness.server.add("GET", BUG_FIELDS, {**meta, "values": [], "isLast": False, "startAt": 4})
    result = harness.run(*BUG)
    assert result.code == 6
    assert result.error["code"] == "metadata_incomplete"
    assert harness.server.writes() == []


@pytest.mark.parametrize("damage", ["short_final_page", "malformed_field", "duplicate_field"])
def test_createmeta_inconsistent_final_page_fails_closed(harness: Harness, damage: str) -> None:
    meta = fixture("createmeta_bug_fields.json")
    if damage == "short_final_page":
        meta["values"] = meta["values"][:-1]
    elif damage == "malformed_field":
        meta["values"][-1] = {"name": "Component owner"}
    else:
        meta["values"][-1]["fieldId"] = meta["values"][-2]["fieldId"]
    setup_create(harness, bug_fields=meta)
    result = harness.run(*BUG)
    assert result.code == 6
    assert result.error["code"] == "metadata_incomplete"
    assert harness.server.writes() == []


def test_createmeta_empty_final_page_without_markers(harness: Harness) -> None:
    meta = fixture("createmeta_bug_fields.json")
    unmarked = {k: v for k, v in meta.items() if k not in ("isLast", "total")}
    setup_create(harness)
    harness.server.routes[("GET", BUG_FIELDS)] = []
    end = len(meta["values"])
    harness.server.add("GET", BUG_FIELDS, {**unmarked, "values": meta["values"][:4]})
    harness.server.add("GET", BUG_FIELDS, {**unmarked, "startAt": 4, "values": meta["values"][4:]})
    harness.server.add("GET", BUG_FIELDS, {**unmarked, "startAt": end, "values": []})
    assert harness.run(*BUG).code == 0
    starts = [r.url.params["startAt"] for r in harness.server.requests if r.url.path == BUG_FIELDS]
    assert starts == ["0", "4", str(end)]


@pytest.mark.parametrize("markers", [{"isLast": False}, {"total": 99}])
def test_createmeta_empty_page_with_markers_still_fails_closed(
    harness: Harness, markers: dict[str, Any]
) -> None:
    meta = fixture("createmeta_bug_fields.json")
    unmarked = {k: v for k, v in meta.items() if k not in ("isLast", "total")}
    setup_create(harness)
    harness.server.routes[("GET", BUG_FIELDS)] = []
    harness.server.add("GET", BUG_FIELDS, {**unmarked, "values": meta["values"][:4]})
    harness.server.add("GET", BUG_FIELDS, {**unmarked, **markers, "startAt": 4, "values": []})
    result = harness.run(*BUG)
    assert result.error["code"] == "metadata_incomplete"
    assert harness.server.writes() == []


@pytest.mark.parametrize("first_markers", [{"total": 99}, {"total": 99, "isLast": False}])
def test_createmeta_earlier_total_bounds_unmarked_empty_page(
    harness: Harness, first_markers: dict[str, Any]
) -> None:
    meta = fixture("createmeta_bug_fields.json")
    unmarked = {k: v for k, v in meta.items() if k not in ("isLast", "total")}
    setup_create(harness)
    harness.server.routes[("GET", BUG_FIELDS)] = []
    harness.server.add(
        "GET", BUG_FIELDS, {**unmarked, **first_markers, "values": meta["values"][:4]}
    )
    harness.server.add("GET", BUG_FIELDS, {**unmarked, "startAt": 4, "values": []})
    result = harness.run(*BUG)
    assert result.code == 6
    assert result.error["code"] == "metadata_incomplete"
    assert result.error["details"]["total"] == 99
    assert harness.server.writes() == []


def test_createmeta_changed_total_fails_closed(harness: Harness) -> None:
    meta = fixture("createmeta_bug_fields.json")
    end = len(meta["values"])
    setup_create(harness)
    harness.server.routes[("GET", BUG_FIELDS)] = []
    first = {**meta, "values": meta["values"][:4], "isLast": False, "total": end + 1}
    harness.server.add("GET", BUG_FIELDS, first)
    harness.server.add(
        "GET",
        BUG_FIELDS,
        {**meta, "values": meta["values"][4:], "isLast": True, "total": end, "startAt": 4},
    )
    result = harness.run(*BUG)
    assert result.error["code"] == "metadata_incomplete"
    assert result.error["details"]["previous_total"] == end + 1
    assert harness.server.writes() == []


def test_create_remote_400_is_sanitized(harness: Harness) -> None:
    setup_create(harness)
    harness.server.routes[("POST", CREATE)] = []
    harness.server.add(
        "POST", CREATE, {"errors": {"customfield_10600": "Environment invalid"}}, status=400
    )
    result = harness.run(*BUG)
    assert result.code == 5
    assert result.error["details"]["fields"] == {"customfield_10600": "Environment invalid"}
    assert "retry" not in result.error["details"]


@pytest.mark.parametrize(
    "body",
    [
        b"",
        [],
        {},
        {"id": "30001"},
        {"id": "30001", "key": ""},
        {"id": "30001", "key": None},
        {"id": "30001", "key": "not a key"},
        {"id": "30001", "key": "OTHER-42"},
        {"id": "30001", "key": "EXAMPLEX-42"},
        {"id": "30001", "key": "EXAMPLE-42\n"},
        {"key": "EXAMPLE-42"},
        {"id": "", "key": "EXAMPLE-42"},
        {"id": 30001, "key": "EXAMPLE-42"},
    ],
)
def test_create_unconfirmed_success_is_outcome_unknown(harness: Harness, body: Any) -> None:
    setup_create(harness)
    harness.server.routes[("POST", CREATE)] = []
    if isinstance(body, bytes):
        harness.server.add("POST", CREATE, handler=lambda _: httpx.Response(201, content=body))
    else:
        harness.server.add("POST", CREATE, body, status=201)
    result = harness.run(*BUG)
    assert result.code == 6, result.out
    assert result.error["code"] == "outcome_unknown"
    assert "data" not in result.out
    assert "retry" not in result.error["details"]
    if body != b"":
        assert "yojo jira search" in result.error["fix"]
        assert "project = EXAMPLE" in result.error["fix"]
        assert "'Login button misaligned'" in result.error["fix"]
    assert len(harness.server.writes()) == 1


def test_create_status_read_failure_still_reports_the_created_issue(harness: Harness) -> None:
    setup_create(harness)
    harness.server.routes[("GET", NEW_ISSUE)] = []
    harness.server.add("GET", NEW_ISSUE, {"errorMessages": ["down"]}, status=503)
    result = harness.run(*BUG)
    assert result.code == 0
    assert result.data["key"] == "EXAMPLE-42"
    assert result.data["status"] is None


# --------------------------------------------------------------------------- issue get


def setup_get(harness: Harness, **fields: Any) -> None:
    harness.write_config()
    harness.store()
    harness.server.add("GET", FIELDS, fixture("fields.json"))
    harness.server.add("GET", ISSUE, issue_reply(**fields))


def test_get_returns_a_compact_issue(harness: Harness) -> None:
    setup_get(harness, assignee={"name": "builder1", "displayName": "Example Builder"})
    result = harness.run("jira", "issue", "get", "EXAMPLE-1")
    assert result.code == 0
    assert result.data == {
        "key": "EXAMPLE-1",
        "url": JIRA + "/browse/EXAMPLE-1",
        "summary": "Login page supports SSO",
        "status": "To Do",
        "issue_type": "Story",
        "project": "EXAMPLE",
        "assignee": "Example Builder",
        "updated": None,
        "fields": [],
        "more": None,
        "content_trust": "untrusted",
    }
    assert FIELDS not in harness.server.paths()
    assert harness.server.writes() == []


def test_get_named_fields_and_trims_long_text(harness: Harness) -> None:
    long = "x" * 5000
    setup_get(harness, description=long, customfield_10002=3.0, priority={"name": "High"})
    result = harness.run(
        "jira", "issue", "get", "EXAMPLE-1", "--fields", "story points, Description,priority"
    )
    assert result.code == 0
    fields = {f["field"]: f for f in result.data["fields"]}
    assert fields["Story Points"]["value"] == 3.0
    assert fields["Priority"]["value"] == "High"
    assert fields["Description"]["truncated"] is True
    assert len(fields["Description"]["value"]) == 2000
    assert result.data["more"] == "yojo jira issue get EXAMPLE-1 --fields Description --full"
    query = harness.server.requests[-1].url.params["fields"].split(",")
    assert {"customfield_10002", "description", "priority"} <= set(query)
    full = harness.run("jira", "issue", "get", "EXAMPLE-1", "--fields", "description", "--full")
    assert full.data["fields"][0]["value"] == long
    assert full.data["more"] is None


def test_get_unknown_field_names_are_all_reported(harness: Harness) -> None:
    setup_get(harness)
    result = harness.run("jira", "issue", "get", "EXAMPLE-1", "--fields", "Severty,Nope")
    assert result.code == 2
    assert result.error["code"] == "input_invalid"
    problems = result.error["details"]["problems"]
    assert [p["given"] for p in problems] == ["Severty", "Nope"]
    assert problems[0]["suggestion"] == "Severity"
    assert ISSUE not in harness.server.paths()


@pytest.mark.parametrize("fields", ["missing", None, "x", []])
def test_get_issue_malformed_fields_is_response_invalid(harness: Harness, fields: Any) -> None:
    harness.write_config()
    harness.store()
    issue = fixture("issue_todo.json")
    if fields == "missing":
        del issue["fields"]
    else:
        issue["fields"] = fields
    harness.server.add("GET", ISSUE, issue)
    result = harness.run("jira", "issue", "get", "EXAMPLE-1")
    assert result.code == 6
    assert result.error["code"] == "response_invalid"
    assert harness.server.paths() == [ISSUE]


def test_get_not_found(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    harness.server.add("GET", ISSUE, {"errorMessages": ["Issue does not exist"]}, status=404)
    result = harness.run("jira", "issue", "get", "EXAMPLE-1")
    assert result.code == 5
    assert result.error["code"] == "not_found"


# --------------------------------------------------------------------------- issue update


def setup_update(harness: Harness, after: dict[str, Any]) -> None:
    harness.write_config()
    harness.store()
    harness.server.add("GET", EDITMETA, fixture("editmeta_bug.json"))
    harness.server.add("PUT", ISSUE, status=204)
    harness.server.add("GET", ISSUE, issue_reply(**after))


def test_update_sets_and_reads_back(harness: Harness) -> None:
    setup_update(
        harness, {"priority": {"id": "2", "name": "High"}, "labels": ["ui"], "summary": "New"}
    )
    result = harness.run(
        "jira",
        "issue",
        "update",
        "EXAMPLE-1",
        "--summary",
        "New",
        "--set",
        "priority=high",
        "--set",
        "Labels=ui",
    )
    assert result.code == 0, result.out
    assert result.data == {
        "key": "EXAMPLE-1",
        "url": JIRA + "/browse/EXAMPLE-1",
        "status": "To Do",
        "changed": [
            {"field": "Summary", "id": "summary"},
            {"field": "Priority", "id": "priority"},
            {"field": "Labels", "id": "labels"},
        ],
    }
    (put,) = harness.server.writes()
    assert body_of(put) == {"fields": {"summary": "New", "priority": {"id": "2"}, "labels": ["ui"]}}


def test_update_read_back_mismatch_is_outcome_unknown(harness: Harness) -> None:
    setup_update(harness, {"priority": {"id": "3", "name": "Medium"}})
    result = harness.run("jira", "issue", "update", "EXAMPLE-1", "--set", "Priority=High")
    assert result.code == 6
    assert result.error["code"] == "outcome_unknown"
    assert result.error["details"]["fields"] == ["Priority"]
    assert "yojo jira issue get EXAMPLE-1 --fields priority" in result.error["fix"]
    assert "retry" not in result.error["details"]
    assert len(harness.server.writes()) == 1


def test_update_read_back_failure_is_outcome_unknown(harness: Harness) -> None:
    setup_update(harness, {})
    harness.server.routes[("GET", ISSUE)] = []
    harness.server.add("GET", ISSUE, {"errorMessages": ["down"]}, status=503)
    result = harness.run("jira", "issue", "update", "EXAMPLE-1", "--set", "Labels=x")
    assert result.code == 6
    assert result.error["code"] == "outcome_unknown"


def test_update_refuses_every_problem_before_writing(harness: Harness) -> None:
    setup_update(harness, {})
    result = harness.run(
        "jira",
        "issue",
        "update",
        "EXAMPLE-1",
        "--set",
        "Reporter=someone",
        "--set",
        "Story Pts=2",
        "--set",
        "Priority=Urgent",
    )
    assert result.code == 4
    found = checks(result.error)
    assert set(found) == {"field:Reporter", "field:Story Pts", "field:Priority"}
    assert found["field:Story Pts"]["details"]["suggestion"] == "Story Points"
    retry = result.error["details"]["retry_argv"]
    assert "Reporter=someone" not in retry
    assert "Story Points=2" in retry
    assert "Priority=<value>" in retry
    assert harness.server.writes() == []


def test_update_needs_a_change(harness: Harness) -> None:
    result = harness.run("jira", "issue", "update", "EXAMPLE-1")
    assert result.code == 2
    assert result.error["code"] == "input_invalid"
    assert harness.server.requests == []


def test_update_input_document_is_not_echoed_in_retry(harness: Harness) -> None:
    setup_update(harness, {})
    doc = json.dumps({"fields": {"Severty": "Major"}}).encode()
    result = harness.run("jira", "issue", "update", "EXAMPLE-1", "--input", "-", stdin=doc)
    assert result.code == 4
    assert result.error["details"]["retry"] is None
    assert result.error["details"]["retry_argv"] is None
    assert harness.server.writes() == []


# --------------------------------------------------------------------------- issue link


def setup_link(harness: Harness, *, links_after: list[dict[str, Any]] | None = None) -> None:
    harness.write_config()
    harness.store()
    harness.server.add("GET", ISSUE, issue_reply(issuelinks=[]))
    if links_after is not None:
        harness.server.add("GET", ISSUE, issue_reply(issuelinks=links_after))
    harness.server.add("GET", ISSUE_2, issue_reply("EXAMPLE-2", issuelinks=[]))
    harness.server.add("GET", LINK_TYPES, fixture("issue_link_types.json"))
    harness.server.add("GET", PERMISSIONS, permissions(LINK_ISSUES=True))
    harness.server.add("POST", LINK, status=201)


def _outward(key: str, type_id: str = "10000") -> dict[str, Any]:
    return {"id": "1", "type": {"id": type_id}, "outwardIssue": {"key": key}}


def test_link_outward_wording(harness: Harness) -> None:
    setup_link(harness, links_after=[_outward("EXAMPLE-2")])
    result = harness.run("jira", "issue", "link", "EXAMPLE-1", "EXAMPLE-2", "--type", "blocks")
    assert result.code == 0, result.out
    assert result.data == {
        "key": "EXAMPLE-1",
        "url": JIRA + "/browse/EXAMPLE-1",
        "relation": "blocks",
        "other": "EXAMPLE-2",
        "link_type": "Blocks",
    }
    (post,) = harness.server.writes()
    assert body_of(post) == {
        "type": {"name": "Blocks"},
        "inwardIssue": {"key": "EXAMPLE-1"},
        "outwardIssue": {"key": "EXAMPLE-2"},
    }


def test_link_inward_wording_swaps_the_source(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    harness.server.add("GET", ISSUE, issue_reply(issuelinks=[]))
    harness.server.add("GET", ISSUE_2, issue_reply("EXAMPLE-2", issuelinks=[]))
    harness.server.add("GET", ISSUE_2, issue_reply("EXAMPLE-2", issuelinks=[_outward("EXAMPLE-1")]))
    harness.server.add("GET", LINK_TYPES, fixture("issue_link_types.json"))
    harness.server.add("GET", PERMISSIONS, permissions(LINK_ISSUES=True))
    harness.server.add("POST", LINK, status=201)
    result = harness.run(
        "jira", "issue", "link", "EXAMPLE-1", "EXAMPLE-2", "--type", "is blocked by"
    )
    assert result.code == 0, result.out
    assert result.data["relation"] == "is blocked by"
    assert body_of(harness.server.writes()[0])["inwardIssue"] == {"key": "EXAMPLE-2"}


def test_link_not_visible_after_post_is_outcome_unknown(harness: Harness) -> None:
    setup_link(harness, links_after=[])
    result = harness.run("jira", "issue", "link", "EXAMPLE-1", "EXAMPLE-2", "--type", "Blocks")
    assert result.code == 6
    assert result.error["code"] == "outcome_unknown"
    assert "yojo jira issue get EXAMPLE-1 --fields issuelinks" in result.error["fix"]
    assert len(harness.server.writes()) == 1


def test_link_unknown_type_suggests_and_lists(harness: Harness) -> None:
    setup_link(harness)
    result = harness.run("jira", "issue", "link", "EXAMPLE-1", "EXAMPLE-2", "--type", "duplicat")
    assert result.code == 4
    check = checks(result.error)["link_type"]
    assert check["details"]["suggestion"] == "Duplicate"
    retry = result.error["details"]["retry_argv"]
    assert retry[retry.index("--type") + 1] == "Duplicate"
    assert harness.server.writes() == []


def test_link_existing_link_and_permission_are_refused(harness: Harness) -> None:
    setup_link(harness)
    harness.server.routes[("GET", ISSUE)] = []
    harness.server.add("GET", ISSUE, issue_reply(issuelinks=[_outward("EXAMPLE-2")]))
    result = harness.run("jira", "issue", "link", "EXAMPLE-1", "EXAMPLE-2", "--type", "blocks")
    assert checks(result.error)["existing_link"]["status"] == "fail"
    harness.server.routes[("GET", PERMISSIONS)] = []
    harness.server.add("GET", PERMISSIONS, permissions(LINK_ISSUES=False))
    result = harness.run("jira", "issue", "link", "EXAMPLE-1", "EXAMPLE-2", "--type", "blocks")
    assert checks(result.error)["permission:LINK_ISSUES"]["status"] == "fail"
    assert harness.server.writes() == []


def test_link_missing_issue_is_reported_with_other_problems(harness: Harness) -> None:
    setup_link(harness)
    harness.server.routes[("GET", ISSUE_2)] = []
    harness.server.add("GET", ISSUE_2, {"errorMessages": ["no"]}, status=404)
    result = harness.run("jira", "issue", "link", "EXAMPLE-1", "EXAMPLE-2", "--type", "zzz")
    found = checks(result.error)
    assert found["issue:EXAMPLE-2"]["status"] == "fail"
    assert found["link_type"]["status"] == "fail"
    assert harness.server.writes() == []


# --------------------------------------------------------------------------- comments


def setup_comment(harness: Harness, *, allowed: bool = True) -> None:
    harness.write_config()
    harness.store()
    harness.server.add("GET", ISSUE, issue_reply())
    harness.server.add("GET", PERMISSIONS, permissions(ADD_COMMENTS=allowed))


def test_comment_from_stdin(harness: Harness) -> None:
    setup_comment(harness)
    harness.server.add("POST", COMMENT, {"id": "777"}, status=201)
    result = harness.run(
        "jira", "comment", "add", "EXAMPLE-1", "--body-file", "-", stdin="Hé\n".encode()
    )
    assert result.data == {
        "key": "EXAMPLE-1",
        "url": JIRA + "/browse/EXAMPLE-1?focusedCommentId=777#comment-777",
        "comment_id": "777",
    }
    assert body_of(harness.server.writes()[0]) == {"body": "Hé\n"}


def test_comment_from_flag(harness: Harness) -> None:
    setup_comment(harness)
    harness.server.add("POST", COMMENT, {"id": "778"}, status=201)
    assert harness.run("jira", "comment", "add", "EXAMPLE-1", "--body", "Looks good").code == 0
    assert body_of(harness.server.writes()[0]) == {"body": "Looks good"}


@pytest.mark.parametrize(
    "body", [b"", [], {}, {"id": None}, {"id": ""}, {"id": " "}, {"id": 777}, {"body": "x"}]
)
def test_comment_unconfirmed_success_is_outcome_unknown(harness: Harness, body: Any) -> None:
    setup_comment(harness)
    if isinstance(body, bytes):
        harness.server.add("POST", COMMENT, handler=lambda _: httpx.Response(201, content=body))
    else:
        harness.server.add("POST", COMMENT, body, status=201)
    result = harness.run("jira", "comment", "add", "EXAMPLE-1", "--body-file", "-", stdin=b"hi")
    assert result.code == 6, result.out
    assert result.error["code"] == "outcome_unknown"
    assert JIRA + "/browse/EXAMPLE-1" in result.error["fix"]
    assert "retry" not in result.error["details"]
    assert len(harness.server.writes()) == 1


def test_comment_empty_and_forbidden_are_refused_together(harness: Harness) -> None:
    setup_comment(harness, allowed=False)
    (harness.tmp_path / "c.txt").write_text("   \n")
    result = harness.run("jira", "comment", "add", "EXAMPLE-1", "--body-file", "c.txt")
    assert result.code == 4
    found = checks(result.error)
    assert found["body"]["status"] == "fail"
    assert found["permission:ADD_COMMENTS"]["status"] == "fail"
    assert harness.server.writes() == []


def test_comment_permission_not_reported_is_unverified(harness: Harness) -> None:
    setup_comment(harness)
    harness.server.routes[("GET", PERMISSIONS)] = []
    harness.server.add("GET", PERMISSIONS, {"permissions": {}})
    result = harness.run("jira", "comment", "add", "EXAMPLE-1", "--body", "x")
    assert result.code == 4
    assert checks(result.error)["permission:ADD_COMMENTS"]["status"] == "unverified"
    assert harness.server.writes() == []


# --------------------------------------------------------------------------- search


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
                    "assignee": {"displayName": "Example Builder"},
                },
            },
            {"id": "2", "key": "EXAMPLE-2", "fields": {}},
        ],
    }
    harness.server.add("GET", SEARCH, body)
    result = harness.run("jira", "search", "--jql", "project = EXAMPLE", "--limit", "2")
    assert result.data["next_start"] == 2
    assert result.data["content_trust"] == "untrusted"
    assert result.data["issues"] == [
        {
            "key": "EXAMPLE-1",
            "summary": "a",
            "status": "To Do",
            "issue_type": "Story",
            "assignee": "Example Builder",
        },
        {"key": "EXAMPLE-2", "summary": None, "status": None, "issue_type": None, "assignee": None},
    ]
    params = harness.server.requests[0].url.params
    assert params["maxResults"] == "2"
    assert params["startAt"] == "0"


def test_search_default_limit_is_bounded(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    harness.server.add("GET", SEARCH, {"total": 0, "issues": []})
    harness.run("jira", "search", "--jql", "x")
    assert harness.server.requests[0].url.params["maxResults"] == "20"


def test_search_invalid_jql_is_remote_validation(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    harness.server.add("GET", SEARCH, {"errorMessages": ["Error in the JQL Query"]}, status=400)
    result = harness.run("jira", "search", "--jql", "project = = X")
    assert result.code == 5
    assert result.error["details"]["messages"] == ["Error in the JQL Query"]


def test_search_next_start_counts_malformed_items(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    body = {
        "startAt": 10,
        "total": 50,
        "issues": [
            {"id": "1", "key": "EXAMPLE-1", "fields": {}},
            "junk",
            {"id": "3", "key": "EXAMPLE-3", "fields": {}},
            {"id": "4", "key": "EXAMPLE-4", "fields": {}},
        ],
    }
    harness.server.add("GET", SEARCH, body)
    result = harness.run("jira", "search", "--jql", "x", "--start", "10", "--limit", "3")
    assert [i["key"] for i in result.data["issues"]] == ["EXAMPLE-1", "EXAMPLE-3"]
    assert result.data["next_start"] == 13
    assert result.data["total"] == 50


def test_search_without_total_uses_raw_count(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    harness.server.add("GET", SEARCH, {"issues": ["junk", "junk"]})
    result = harness.run("jira", "search", "--jql", "x", "--start", "4", "--limit", "2")
    assert result.data["issues"] == []
    assert result.data["total"] == 6
    assert result.data["next_start"] is None


@pytest.mark.parametrize("fields", ["missing", None, "x", [], 3])
def test_search_malformed_fields_is_response_invalid(harness: Harness, fields: Any) -> None:
    harness.write_config()
    harness.store()
    item: dict[str, Any] = {"id": "1", "key": "EXAMPLE-1"}
    if fields != "missing":
        item["fields"] = fields
    harness.server.add("GET", SEARCH, {"total": 1, "issues": [item]})
    result = harness.run("jira", "search", "--jql", "x")
    assert result.code == 6
    assert result.error["code"] == "response_invalid"
    assert harness.server.writes() == []


def test_search_raw_empty_page_ends_despite_advertised_total(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    harness.server.add("GET", SEARCH, {"startAt": 20, "total": 500, "issues": []})
    result = harness.run("jira", "search", "--jql", "x", "--start", "20", "--limit", "10")
    assert result.code == 0
    assert result.data["issues"] == []
    assert result.data["next_start"] is None


def test_jql_quote_escapes() -> None:
    assert jql_quote('a "b" \\c') == '"a \\"b\\" \\\\c"'


# --------------------------------------------------------------------------- config check


STATUSES = jira_path("/rest/api/2/project/EXAMPLE/statuses")


def _statuses(*names: str) -> list[dict[str, Any]]:
    return [{"name": "Story", "statuses": [{"name": n} for n in names]}]


def setup_config_check(harness: Harness, config: str | None = None) -> None:
    harness.write_config(config or config_text())
    harness.store()
    harness.server.add("GET", FIELDS, fixture("fields.json"))
    harness.server.add("GET", STATUSES, _statuses("To Do", "In Progress", "Done"))


def test_config_check_pass(harness: Harness) -> None:
    setup_config_check(harness)
    result = harness.run("config", "check")
    assert result.code == 0, result.out
    assert result.data["status"] == "pass"
    ids = {c["id"] for c in result.data["checks"]}
    assert "field:review_flag" in ids
    assert "EXAMPLE:transition:done:to" in ids
    assert harness.server.writes() == []


def test_config_check_drift_on_alias_and_status(harness: Harness) -> None:
    config = config_text().replace("{jira_name: Review Flag}", "{jira_name: Review Flg}")
    setup_config_check(harness, config)
    harness.server.routes[("GET", STATUSES)] = []
    harness.server.add("GET", STATUSES, _statuses("To Do", "Doing", "Done"))
    result = harness.run("config", "check")
    assert result.code == 4
    assert result.error["code"] == "config_drift"
    drift = {c["id"]: c for c in result.error["details"]["checks"] if c["status"] == "drift"}
    assert "Did you mean 'Review Flag'?" in drift["field:review_flag"]["message"]
    assert "EXAMPLE:transition:in_progress:to" in drift


def test_config_check_create_requirements(harness: Harness) -> None:
    config = config_text().replace(
        "      transitions:\n",
        "      create:\n        Bug:\n          required: [Severity, Nonexistent]\n"
        "      transitions:\n",
        1,
    )
    setup_config_check(harness, config)
    harness.server.add("GET", ISSUETYPES, fixture("createmeta_issuetypes.json"))
    result = harness.run("config", "check")
    drift = {c["id"] for c in result.error["details"]["checks"] if c["status"] == "drift"}
    assert drift == {"EXAMPLE:create:Bug:field:Nonexistent"}


def test_config_check_unknown_project_is_drift(harness: Harness) -> None:
    setup_config_check(harness)
    harness.server.routes[("GET", STATUSES)] = []
    harness.server.add("GET", STATUSES, {"errorMessages": ["No project"]}, status=404)
    result = harness.run("config", "check")
    assert result.error["code"] == "config_drift"
    assert "EXAMPLE:project" in {c["id"] for c in result.error["details"]["checks"]}


def test_config_check_needs_a_config(harness: Harness) -> None:
    result = harness.run("config", "check", "--base-url", JIRA)
    assert result.code == 2
    assert result.error["code"] == "config_not_found"
    assert harness.server.requests == []


# --------------------------------------------------------------------------- attachments


ATTACH = jira_path("/rest/api/2/issue/EXAMPLE-1/attachments")


def setup_attach(harness: Harness, limit: int = 10, server_limit: int = 1000) -> None:
    harness.write_config(config_text(max_bytes=limit))
    harness.store()
    add_attach_preflight(harness.server, server_limit)


def add_attach_preflight(server: FakeServer, server_limit: int = 1000) -> None:
    server.add("GET", ISSUE, fixture("issue_todo.json"))
    server.add("GET", PERMISSIONS, permissions(CREATE_ATTACHMENTS=True))
    server.add("GET", ATTACH_META, {"enabled": True, "uploadLimit": server_limit})


def _attachment_reply(body: Any, *, status: int = 200) -> Any:
    def respond(request: httpx.Request) -> httpx.Response:
        request.read()
        if isinstance(body, bytes):
            return httpx.Response(status, content=body)
        return httpx.Response(status, json=body)

    return respond


_attachment_ok = _attachment_reply([{"id": "500", "filename": "f.txt", "size": 10}])


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
    (request,) = harness.server.writes()
    assert request.headers["x-atlassian-token"] == "no-check"
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert int(request.headers["content-length"]) == len(request.content)
    assert b'filename="f.txt"' in request.content
    assert b"\r\n\r\nxxxxxxxxxx\r\n--" in request.content


def test_attachment_growing_while_read_is_rejected_before_sending(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_attach(harness)
    path = harness.tmp_path / "f.txt"
    path.write_bytes(b"x" * 5)
    real_fstat = os.fstat

    def fstat_then_grow(fd: int) -> os.stat_result:
        declared = real_fstat(fd)
        with path.open("ab") as handle:
            handle.write(b"y")
        return declared

    monkeypatch.setattr(os, "fstat", fstat_then_grow)
    harness.server.add("POST", ATTACH, handler=_attachment_ok)
    result = harness.run("jira", "attachment", "add", "EXAMPLE-1", "--file", "f.txt")
    assert result.code == 2
    assert result.error["code"] == "file_invalid"
    assert "changed while yojo was reading it" in result.error["message"]
    assert harness.server.writes() == []


def test_attachment_stream_guard_aborts_body_that_grows(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_attach(harness)
    (harness.tmp_path / "f.txt").write_bytes(b"x" * 5)
    real_snapshot = jira_module._snapshot

    def snapshot_then_grow(path: Path, fd: int, opened: os.stat_result, handle: Any) -> None:
        real_snapshot(path, fd, opened, handle)
        handle.seek(0, os.SEEK_END)
        handle.write(b"y" * 100)
        handle.seek(0)

    monkeypatch.setattr(jira_module, "_snapshot", snapshot_then_grow)
    harness.server.add("POST", ATTACH, handler=_attachment_ok)
    result = harness.run("jira", "attachment", "add", "EXAMPLE-1", "--file", "f.txt")
    assert result.code == 6
    assert result.error["code"] == "outcome_unknown"
    assert result.error["details"]["dispatched"] == "aborted"
    assert result.error["details"]["declared_size"] == 5
    assert result.error["details"]["reason"] == "file grew while uploading"
    assert "issue view" in result.error["fix"]
    # The mock transport buffers the body before routing; the abort happens while buffering.
    assert harness.server.writes() == []


def test_attachment_server_limit(harness: Harness) -> None:
    setup_attach(harness)
    (harness.tmp_path / "f.txt").write_bytes(b"x")
    harness.server.add("POST", ATTACH, status=413, body={"errorMessages": ["too big"]})
    result = harness.run("jira", "attachment", "add", "EXAMPLE-1", "--file", "f.txt")
    assert result.code == 5
    assert result.error["code"] == "remote_limit_exceeded"
    assert len(harness.server.writes()) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="quotes are invalid in Windows names")
def test_attachment_filename_header_is_escaped(harness: Harness) -> None:
    setup_attach(harness)
    (harness.tmp_path / 'we"ird.txt').write_bytes(b"x")
    reply = _attachment_reply([{"id": "501", "filename": "we_ird.txt", "size": 1}])
    harness.server.add("POST", ATTACH, handler=reply)
    assert harness.run("jira", "attachment", "add", "EXAMPLE-1", "--file", 'we"ird.txt').code == 0
    assert b'filename="we_ird.txt"' in harness.server.writes()[0].content


def test_attachment_success_returns_every_confirmed_item(harness: Harness) -> None:
    setup_attach(harness)
    (harness.tmp_path / "f.txt").write_bytes(b"x" * 10)
    items = [
        {"id": "499", "filename": "older.txt", "size": 3},
        {"id": "500", "filename": "f.txt", "size": 10},
    ]
    harness.server.add("POST", ATTACH, handler=_attachment_reply(items))
    result = harness.run("jira", "attachment", "add", "EXAMPLE-1", "--file", "f.txt")
    assert result.code == 0, result.out
    assert result.data == {
        "key": "EXAMPLE-1",
        "url": JIRA + "/browse/EXAMPLE-1",
        "attachments": items,
    }


GOOD_ITEM = {"id": "500", "filename": "f.txt", "size": 10}


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"[{",
        [],
        {},
        GOOD_ITEM,
        ["f.txt"],
        [{**GOOD_ITEM, "id": None}],
        [{**GOOD_ITEM, "id": 500}],
        [{**GOOD_ITEM, "id": " "}],
        [{k: v for k, v in GOOD_ITEM.items() if k != "id"}],
        [{k: v for k, v in GOOD_ITEM.items() if k != "filename"}],
        [{**GOOD_ITEM, "filename": ""}],
        [{k: v for k, v in GOOD_ITEM.items() if k != "size"}],
        [{**GOOD_ITEM, "size": True}],
        [{**GOOD_ITEM, "size": -1}],
        [{**GOOD_ITEM, "size": "10"}],
        [{**GOOD_ITEM, "size": 10.0}],
        [{**GOOD_ITEM, "filename": "other.txt"}],
        [{**GOOD_ITEM, "size": 9}],
        [GOOD_ITEM, {"id": "501", "filename": "g.txt"}],
        [GOOD_ITEM, None],
    ],
)
def test_attachment_incomplete_success_is_outcome_unknown(harness: Harness, body: Any) -> None:
    setup_attach(harness)
    (harness.tmp_path / "f.txt").write_bytes(b"x" * 10)
    harness.server.add("POST", ATTACH, handler=_attachment_reply(body))
    result = harness.run("jira", "attachment", "add", "EXAMPLE-1", "--file", "f.txt")
    assert result.code == 6, result.out
    assert result.error["code"] == "outcome_unknown"
    assert "data" not in result.out
    assert "issue view" in result.error["fix"]
    if body not in (b"not json", b"[{"):
        assert "'f.txt'" in result.error["fix"]
        assert "10 bytes" in result.error["fix"]
    assert len(harness.server.writes()) == 1


def test_attachment_incomplete_success_marks_write_outcome_unknown(tmp_path: Path) -> None:
    config_path = tmp_path / "yojo.yaml"
    config_path.write_text(config_text())
    jira_config = parse_config_file(config_path).jira
    assert jira_config is not None
    upload = tmp_path / "f.txt"
    upload.write_bytes(b"x" * 10)
    server = FakeServer().add("POST", ATTACH, handler=_attachment_reply([]))
    add_attach_preflight(server)
    client = ServiceClient(
        "jira", JIRA, Credential(auth="pat", secret=JIRA_TOKEN), transport=server.transport()
    )
    with pytest.raises(YojoError) as info:
        attachment_add(client, jira_config, "EXAMPLE-1", upload)
    assert info.value.code == "outcome_unknown"
    assert info.value.write_outcome == "unknown"


def _open_then(path: Path, action: Any) -> Any:
    real_open = os.open

    def patched(target: Any, *args: Any, **kwargs: Any) -> Any:
        if os.fspath(target) == os.fspath(path):
            action()
        return real_open(target, *args, **kwargs)

    return patched


def test_attachment_grown_past_limit_before_open_is_rejected(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_attach(harness)
    path = harness.tmp_path / "f.txt"
    path.write_bytes(b"x" * 5)
    monkeypatch.setattr(os, "open", _open_then(path, lambda: path.write_bytes(b"x" * 11)))
    harness.server.add("POST", ATTACH, handler=_attachment_ok)
    result = harness.run("jira", "attachment", "add", "EXAMPLE-1", "--file", "f.txt")
    assert result.code == 2
    assert result.error["code"] == "attachment_too_large"
    assert result.error["details"] == {"size": 11, "limit": 10}
    assert harness.server.requests == []


def test_attachment_replaced_by_non_regular_file_is_rejected(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_attach(harness)
    (harness.tmp_path / "f.txt").write_bytes(b"x")
    real_fstat = os.fstat

    def fifo_fstat(fd: int) -> os.stat_result:
        info = list(real_fstat(fd))
        info[0] = stat.S_IFIFO | 0o644
        return os.stat_result(info)

    monkeypatch.setattr(os, "fstat", fifo_fstat)
    harness.server.add("POST", ATTACH, handler=_attachment_ok)
    result = harness.run("jira", "attachment", "add", "EXAMPLE-1", "--file", "f.txt")
    assert result.code == 2
    assert result.error["code"] == "file_invalid"
    assert harness.server.requests == []


def test_attachment_fstat_failure_is_file_invalid(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_attach(harness)
    path = harness.tmp_path / "f.txt"
    path.write_bytes(b"x")
    target = path.stat().st_ino
    real_fstat = os.fstat

    # Fail only for the attachment; a global failure would also break pytest's own capture.
    def broken(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        if info.st_ino == target:
            raise OSError("gone")
        return info

    monkeypatch.setattr(os, "fstat", broken)
    result = harness.run("jira", "attachment", "add", "EXAMPLE-1", "--file", "f.txt")
    assert result.error["code"] == "file_invalid"
    assert harness.server.requests == []


def test_attachment_uses_opened_file_size(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_attach(harness)
    path = harness.tmp_path / "f.txt"
    path.write_bytes(b"x" * 5)
    monkeypatch.setattr(os, "open", _open_then(path, lambda: path.write_bytes(b"y" * 8)))
    reply = _attachment_reply([{"id": "500", "filename": "f.txt", "size": 8}])
    harness.server.add("POST", ATTACH, handler=reply)
    result = harness.run("jira", "attachment", "add", "EXAMPLE-1", "--file", "f.txt")
    assert result.code == 0, result.out
    (request,) = harness.server.writes()
    assert int(request.headers["content-length"]) == len(request.content)
    assert b"\r\n\r\nyyyyyyyy\r\n--" in request.content


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
def test_attachment_swapped_for_real_fifo_does_not_block(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_attach(harness)
    path = harness.tmp_path / "f.txt"
    path.write_bytes(b"x")

    def swap() -> None:
        path.unlink()
        getattr(os, "mkfifo")(path)  # noqa: B009 - absent from Windows os and typeshed

    def hung(signum: int, frame: Any) -> None:
        raise TimeoutError("open() blocked on the FIFO")

    monkeypatch.setattr(os, "open", _open_then(path, swap))
    # These APIs are intentionally looked up only in this POSIX-only test.
    alarm = getattr(signal, "SIGALRM")  # noqa: B009 - absent from Windows typeshed
    timer = getattr(signal, "ITIMER_REAL")  # noqa: B009 - absent from Windows typeshed
    setitimer = getattr(signal, "setitimer")  # noqa: B009 - absent from Windows typeshed
    previous = signal.signal(alarm, hung)
    setitimer(timer, 5)
    try:
        result = harness.run("jira", "attachment", "add", "EXAMPLE-1", "--file", "f.txt")
    finally:
        setitimer(timer, 0)
        signal.signal(alarm, previous)
    assert result.code == 2
    assert result.error["code"] == "file_invalid"
    assert result.error["message"].startswith("Not a regular file")
    assert harness.server.requests == []


def test_attachment_same_size_replacement_before_open_is_rejected(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_attach(harness)
    path = harness.tmp_path / "f.txt"
    path.write_bytes(b"x" * 5)
    other = harness.tmp_path / "other.txt"
    other.write_bytes(b"y" * 5)
    monkeypatch.setattr(os, "open", _open_then(path, lambda: os.replace(other, path)))
    harness.server.add("POST", ATTACH, handler=_attachment_ok)
    result = harness.run("jira", "attachment", "add", "EXAMPLE-1", "--file", "f.txt")
    assert result.code == 2
    assert result.error["code"] == "file_invalid"
    assert "was replaced" in result.error["message"]
    assert harness.server.requests == []


def test_attachment_uploads_snapshot_despite_in_stream_rewrite(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_attach(harness)
    path = harness.tmp_path / "f.txt"
    path.write_bytes(b"x" * 5)
    real_multipart = jira_module._multipart

    def rewrite_then_stream(*args: Any) -> Any:
        with path.open("r+b") as handle:
            handle.write(b"y" * 5)
        return real_multipart(*args)

    monkeypatch.setattr(jira_module, "_multipart", rewrite_then_stream)
    harness.server.add(
        "POST", ATTACH, handler=_attachment_reply([{"id": "500", "filename": "f.txt", "size": 5}])
    )
    result = harness.run("jira", "attachment", "add", "EXAMPLE-1", "--file", "f.txt")
    assert result.code == 0, result.out
    (request,) = harness.server.writes()
    assert b"\r\n\r\nxxxxx\r\n--" in request.content
    assert path.read_bytes() == b"yyyyy"


def test_attachment_preflight_reports_every_remote_problem(harness: Harness) -> None:
    setup_attach(harness, server_limit=4)
    harness.server.routes[("GET", PERMISSIONS)] = []
    harness.server.add("GET", PERMISSIONS, permissions(CREATE_ATTACHMENTS=False))
    (harness.tmp_path / "f.txt").write_bytes(b"x" * 5)
    result = harness.run("jira", "attachment", "add", "EXAMPLE-1", "--file", "f.txt")
    assert result.code == 4
    found = checks(result.error)
    assert found["permission:CREATE_ATTACHMENTS"]["status"] == "fail"
    assert found["server_limit"]["details"] == {"size": 5, "limit": 4}
    assert harness.server.writes() == []


def test_attachment_disabled_is_refused(harness: Harness) -> None:
    setup_attach(harness)
    harness.server.routes[("GET", ATTACH_META)] = []
    harness.server.add("GET", ATTACH_META, {"enabled": False})
    (harness.tmp_path / "f.txt").write_bytes(b"x")
    result = harness.run("jira", "attachment", "add", "EXAMPLE-1", "--file", "f.txt")
    assert checks(result.error)["attachments_enabled"]["status"] == "fail"
    assert harness.server.writes() == []
