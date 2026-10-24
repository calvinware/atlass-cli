from __future__ import annotations

import json
import os
import signal
import stat
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from atlass_cli import jira as jira_module
from atlass_cli.auth import Credential
from atlass_cli.config import JiraConfig, parse_config_file
from atlass_cli.contracts import AtlError
from atlass_cli.features import (
    CANDIDATE_POOL,
    WEIGHT_EXACT_KEY,
    WEIGHT_TOKEN,
    jql_quote,
    rank,
    tokenize,
)
from atlass_cli.jira import attachment_add, feature_create
from atlass_cli.transport import ServiceClient
from tests.conftest import (
    JIRA,
    JIRA_TOKEN,
    FakeServer,
    Harness,
    config_text,
    fixture,
    jira_path,
)

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
    story = fixture("issue_todo.json")
    story["key"] = "EXAMPLE-10"
    harness.server.add("GET", FEATURE_ISSUE, story)
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


def test_story_create_rejects_two_fields_resolving_to_one_id(harness: Harness) -> None:
    setup_create(harness)
    harness.write_config(
        config_text().replace(
            "{jira_name: Parent Link, encoding: issue_key_string}",
            "{id: customfield_10100, encoding: issue_key_string}",
        )
    )
    result = create_story(harness, STORY)
    assert result.code == 4
    assert result.error["code"] == "requirements_unmet"
    check = checks(result.error)["field:feature_link"]
    assert check["status"] == "fail"
    assert check["details"] == {
        "friendly": ["acceptance_criteria", "feature_link"],
        "field_id": "customfield_10100",
    }
    assert "'acceptance_criteria' and 'feature_link'" in check["message"]
    assert "customfield_10100" in check["message"]
    assert harness.server.writes() == []


def test_story_create_rejects_field_colliding_with_description(harness: Harness) -> None:
    setup_create(harness)
    harness.write_config(
        config_text().replace(
            "work_started: {jira_name: Work Started, encoding: single_option}",
            "work_started: {jira_name: Work Started, encoding: single_option}\n"
            "        notes: {id: description, encoding: text}",
        )
    )
    result = create_story(harness, {**STORY, "description": "d", "fields": {"notes": "n"}})
    assert result.code == 4
    check = checks(result.error)["field:description"]
    assert check["details"]["friendly"] == ["notes", "description"]
    assert harness.server.writes() == []


@pytest.mark.parametrize(
    "mapping",
    [
        "{id: summary, encoding: text}",
        "{jira_name: Summary, encoding: text}",
        "{id: project, encoding: text}",
        "{id: issuetype, encoding: text}",
    ],
)
@pytest.mark.parametrize("command", ["story", "feature"])
def test_create_rejects_friendly_field_mapped_to_fixed_field(
    harness: Harness, mapping: str, command: str
) -> None:
    setup_create(harness, policy="automatic")
    harness.write_config(
        config_text(policy="automatic").replace(
            "work_started: {jira_name: Work Started, encoding: single_option}",
            "work_started: {jira_name: Work Started, encoding: single_option}\n"
            f"        title: {mapping}",
        )
    )
    doc = {**STORY, "fields": {"title": "Overridden"}}
    if command == "feature":
        doc = {"project": "EXAMPLE", "summary": "Add SSO button", "fields": {"title": "Overridden"}}
    result = harness.run("jira", command, "create", "--input", "-", stdin=json.dumps(doc).encode())
    assert result.code == 4
    assert result.error["code"] == "requirements_unmet"
    check = checks(result.error)["field:title"]
    assert check["status"] == "fail"
    assert check["details"]["field_id"] in {"summary", "project", "issuetype"}
    assert harness.server.writes() == []


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
        {"key": "EXAMPLE-42"},
        {"id": "", "key": "EXAMPLE-42"},
        {"id": 30001, "key": "EXAMPLE-42"},
    ],
)
@pytest.mark.parametrize("kind", ["story", "feature"])
def test_create_unconfirmed_success_is_outcome_unknown(
    harness: Harness, kind: str, body: Any
) -> None:
    setup_create(harness, policy="automatic")
    harness.server.routes[("POST", CREATE)] = []
    if isinstance(body, bytes):
        harness.server.add("POST", CREATE, handler=lambda _: httpx.Response(201, content=body))
    else:
        harness.server.add("POST", CREATE, body, status=201)
    if kind == "story":
        result = create_story(harness, STORY)
    else:
        result = create_feature(harness, {"project": "EXAMPLE", "summary": "Add SSO button"})
    assert result.code == 6, result.out
    assert result.error["code"] == "outcome_unknown"
    assert "data" not in result.out
    if body != b"":
        assert "atl jira search" in result.error["fix"]
        assert "project = EXAMPLE" in result.error["fix"]
        assert "'Add SSO button'" in result.error["fix"]
    assert len(harness.server.writes()) == 1


def test_story_create_feature_in_unconfigured_project(harness: Harness) -> None:
    setup_create(harness)
    feature = fixture("issue_feature.json")
    feature["fields"]["project"]["key"] = "OTHER"
    harness.server.routes[("GET", FEATURE_ISSUE)] = []
    harness.server.add("GET", FEATURE_ISSUE, feature)
    result = create_story(harness, STORY)
    assert result.code == 4
    check = checks(result.error)["feature_key"]
    assert check["status"] == "fail"
    assert "OTHER" in check["message"]
    assert check["details"] == {"project": "OTHER"}
    assert harness.server.writes() == []


def test_story_create_feature_key_mismatch(harness: Harness) -> None:
    setup_create(harness)
    feature = fixture("issue_feature.json")
    feature["key"] = "EXAMPLE-11"
    harness.server.routes[("GET", FEATURE_ISSUE)] = []
    harness.server.add("GET", FEATURE_ISSUE, feature)
    result = create_story(harness, STORY)
    assert result.code == 4
    check = checks(result.error)["feature_key"]
    assert check["status"] == "fail"
    assert check["details"] == {"returned_key": "EXAMPLE-11"}
    assert harness.server.writes() == []


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


TEAM_FIELD = {
    "fieldId": "customfield_10900",
    "name": "Team",
    "required": False,
    "hasDefaultValue": False,
    "schema": {"type": "string"},
    "operations": ["set"],
}


def team_config(*, story: str = "[]", feature: str = "[]", policy: str = "automatic") -> str:
    """Return the base config with a ``team`` field and the given per-type create lists."""
    text = config_text(policy=policy)
    block = (
        "      create:\n"
        "        story:\n"
        "          required: [acceptance_criteria, feature_link]\n"
        "        feature:\n"
        "          required: []\n"
    )
    assert block in text
    return text.replace(
        "work_started: {jira_name: Work Started, encoding: single_option}",
        "work_started: {jira_name: Work Started, encoding: single_option}\n"
        "        team: {jira_name: Team, encoding: text}",
    ).replace(
        block,
        f"      create:\n        story:\n          required: {story}\n"
        f"        feature:\n          required: {feature}\n",
    )


def with_team(name: str) -> dict[str, Any]:
    meta: dict[str, Any] = fixture(name)
    meta["values"].append(TEAM_FIELD)
    meta["total"] = len(meta["values"])
    return meta


def setup_feature_required(harness: Harness, *, on_screen: bool) -> None:
    setup_create(harness, policy="automatic")
    harness.write_config(team_config(feature="[team]"))
    meta = (
        with_team("createmeta_feature_fields.json")
        if on_screen
        else fixture("createmeta_feature_fields.json")
    )
    harness.server.routes[("GET", FEATURE_FIELDS)] = []
    harness.server.add("GET", FEATURE_FIELDS, meta)


def test_feature_create_enforces_feature_required_on_screen(harness: Harness) -> None:
    setup_feature_required(harness, on_screen=True)
    result = create_feature(harness, {"project": "EXAMPLE", "summary": "SSO"})
    assert result.code == 4
    assert result.error["code"] == "requirements_unmet"
    check = checks(result.error)["required:team"]
    assert check["status"] == "fail"
    assert "create.feature.required" in check["message"]
    assert "'team'" in check["fix"]
    assert harness.server.writes() == []


@pytest.mark.parametrize("value", ["", "   "])
def test_feature_create_rejects_blank_feature_required(harness: Harness, value: str) -> None:
    setup_feature_required(harness, on_screen=True)
    doc = {"project": "EXAMPLE", "summary": "SSO", "fields": {"team": value}}
    result = create_feature(harness, doc)
    assert result.code == 4
    assert checks(result.error)["required:team"]["status"] == "fail"
    assert harness.server.writes() == []


def test_feature_create_with_feature_required_supplied(harness: Harness) -> None:
    setup_feature_required(harness, on_screen=True)
    doc = {"project": "EXAMPLE", "summary": "SSO", "fields": {"team": "Identity"}}
    assert create_feature(harness, doc).code == 0
    (post,) = harness.server.writes()
    assert json.loads(post.content)["fields"]["customfield_10900"] == "Identity"


def test_feature_create_required_field_off_screen_fails(harness: Harness) -> None:
    setup_feature_required(harness, on_screen=False)
    result = create_feature(harness, {"project": "EXAMPLE", "summary": "SSO"})
    assert result.code == 4
    check = checks(result.error)["required:team"]
    assert check["status"] == "fail"
    assert "not on the Feature create screen" in check["message"]
    assert "create.feature.required" in check["fix"]
    assert harness.server.writes() == []


def test_feature_create_supplied_off_screen_field_fails(harness: Harness) -> None:
    setup_feature_required(harness, on_screen=False)
    doc = {"project": "EXAMPLE", "summary": "SSO", "fields": {"team": "Identity"}}
    result = create_feature(harness, doc)
    assert result.code == 4
    assert checks(result.error)["field:team"]["status"] == "fail"
    assert harness.server.writes() == []


def test_feature_create_ignores_story_required(harness: Harness) -> None:
    setup_create(harness, policy="automatic")
    harness.write_config(team_config(story="[acceptance_criteria, feature_link, team]"))
    result = create_feature(harness, {"project": "EXAMPLE", "summary": "SSO"})
    assert result.code == 0, result.out
    assert not any(c["id"].startswith("required:") for c in result.data["checks"])


def test_feature_create_still_requires_approval_with_empty_feature_required(
    harness: Harness,
) -> None:
    setup_create(harness)
    harness.write_config(team_config(policy="human_confirmed"))
    result = create_feature(harness, {"project": "EXAMPLE", "summary": "SSO"})
    assert result.code == 4
    assert result.error["code"] == "approval_required"
    assert harness.server.requests == []


def test_story_create_ignores_feature_required(harness: Harness) -> None:
    setup_create(harness)
    harness.write_config(team_config(feature="[team]"))
    result = create_story(harness, STORY)
    assert result.code == 0, result.out
    assert not any(c["id"].startswith("required:") for c in result.data["checks"])


def test_story_create_enforces_story_required(harness: Harness) -> None:
    setup_create(harness, story_fields=with_team("createmeta_story_fields.json"))
    harness.write_config(team_config(story="[team]"))
    result = create_story(harness, STORY)
    assert result.code == 4
    check = checks(result.error)["required:team"]
    assert check["status"] == "fail"
    assert "create.story.required" in check["message"]
    assert harness.server.writes() == []
    harness.server.requests.clear()
    assert create_story(harness, {**STORY, "fields": {"team": "Identity"}}).code == 0
    (post,) = harness.server.writes()
    assert json.loads(post.content)["fields"]["customfield_10900"] == "Identity"


def test_story_create_story_required_off_screen_fails(harness: Harness) -> None:
    setup_create(harness)
    harness.write_config(team_config(story="[team]"))
    result = create_story(harness, STORY)
    assert result.code == 4
    check = checks(result.error)["required:team"]
    assert "not on the Story create screen" in check["message"]
    assert "create.story.required" in check["fix"]
    assert harness.server.writes() == []


@pytest.mark.parametrize("missing", ["acceptance_criteria", "feature_key"])
def test_story_builtins_apply_with_empty_story_required(harness: Harness, missing: str) -> None:
    setup_create(harness)
    harness.write_config(team_config())
    result = create_story(harness, {k: v for k, v in STORY.items() if k != missing})
    assert result.code == 4
    assert checks(result.error)[missing]["status"] == "fail"
    assert harness.server.writes() == []


# --------------------------------------------------------------------------- feature find


def setup_find(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    search_body = fixture("search_features.json")

    def search_response(request: httpx.Request) -> httpx.Response:
        jql = request.url.params["jql"]
        if "key in (" not in jql:
            return httpx.Response(200, json=search_body)
        matches = [issue for issue in search_body["issues"] if f'"{issue["key"]}"' in jql]
        start = int(request.url.params["startAt"])
        size = int(request.url.params["maxResults"])
        return httpx.Response(
            200, json={"issues": matches[start : start + size], "total": len(matches)}
        )

    harness.server.add("GET", SEARCH, handler=search_response)
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
    (request,) = harness.server.requests
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
    assert "changed while atl was reading it" in result.error["message"]
    assert harness.server.requests == []


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
    reply = _attachment_reply([{"id": "501", "filename": "we_ird.txt", "size": 1}])
    harness.server.add("POST", ATTACH, handler=reply)
    assert harness.run("jira", "attachment", "add", "EXAMPLE-1", "--file", 'we"ird.txt').code == 0
    assert b'filename="we_ird.txt"' in harness.server.requests[0].content


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
    assert result.data == {"key": "EXAMPLE-1", "attachments": items}


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
    config_path = tmp_path / "atl.yaml"
    config_path.write_text(config_text())
    jira_config = parse_config_file(config_path).jira
    assert jira_config is not None
    upload = tmp_path / "f.txt"
    upload.write_bytes(b"x" * 10)
    server = FakeServer().add("POST", ATTACH, handler=_attachment_reply([]))
    client = ServiceClient(
        "jira", JIRA, Credential(auth="pat", secret=JIRA_TOKEN), transport=server.transport()
    )
    with pytest.raises(AtlError) as info:
        attachment_add(client, jira_config, "EXAMPLE-1", upload)
    assert info.value.code == "outcome_unknown"
    assert info.value.write_outcome == "unknown"


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


@pytest.mark.parametrize(
    "body", [b"", [], {}, {"id": None}, {"id": ""}, {"id": " "}, {"id": 777}, {"body": "x"}]
)
def test_comment_unconfirmed_success_is_outcome_unknown(harness: Harness, body: Any) -> None:
    harness.write_config()
    harness.store()
    path = jira_path("/rest/api/2/issue/EXAMPLE-1/comment")
    if isinstance(body, bytes):
        harness.server.add("POST", path, handler=lambda _: httpx.Response(201, content=body))
    else:
        harness.server.add("POST", path, body, status=201)
    result = harness.run("jira", "comment", "add", "EXAMPLE-1", "--body-file", "-", stdin=b"hi")
    assert result.code == 6, result.out
    assert result.error["code"] == "outcome_unknown"
    assert "issue view" in result.error["fix"]
    assert "EXAMPLE-1" in result.error["fix"]
    assert len(harness.server.writes()) == 1


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


ISSUE_1 = jira_path("/rest/api/2/issue/EXAMPLE-1")
ISSUE_2 = jira_path("/rest/api/2/issue/EXAMPLE-2")
SAMPLES = {"To Do": "EXAMPLE-1", "In Progress": "EXAMPLE-2"}


def _search_hit(key: str, status: str) -> dict[str, Any]:
    return {
        "key": key,
        "fields": {"status": {"name": status}, "project": {"key": "EXAMPLE"}},
    }


def _search_by_status(samples: dict[str, str]) -> Any:
    """Answer the sample search with the configured issue for the JQL's status, if any."""

    def respond(request: httpx.Request) -> httpx.Response:
        jql = request.url.params["jql"]
        hits = [_search_hit(key, s) for s, key in samples.items() if f'status = "{s}"' in jql]
        return httpx.Response(200, json={"issues": hits})

    return respond


def add_samples(harness: Harness, *, samples: dict[str, str] | None = None) -> None:
    harness.server.add(
        "GET", SEARCH, handler=_search_by_status(SAMPLES if samples is None else samples)
    )
    harness.server.add("GET", ISSUE_1, fixture("issue_todo.json"))
    harness.server.add("GET", f"{ISSUE_1}/transitions", fixture("transitions_todo.json"))
    in_progress = fixture("issue_in_progress.json")
    in_progress["key"] = "EXAMPLE-2"
    harness.server.add("GET", ISSUE_2, in_progress)
    harness.server.add("GET", f"{ISSUE_2}/transitions", fixture("transitions_in_progress.json"))


def setup_config_check(harness: Harness, *, sample_found: bool = True) -> None:
    setup_create(harness)
    add_samples(harness, samples=SAMPLES if sample_found else {})


def statuses_of(result: Any) -> dict[str, str]:
    checks = result.data["checks"] if result.code == 0 else result.error["details"]["checks"]
    return {c["id"]: c["status"] for c in checks}


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
    assert result.code == 0, result.out
    checks = {c["id"]: c for c in result.data["checks"]}
    todo = "EXAMPLE:transition:in_progress:To Do"
    in_progress = "EXAMPLE:transition:done:In Progress"
    assert checks[f"{todo}:source_status"]["status"] == "pass"
    assert checks[f"{todo}:transition_available"]["status"] == "pass"
    assert checks[f"{todo}:auto_set:work_started"]["status"] == "pass"
    assert checks[f"{todo}:transition_available"]["details"]["sample_issue"] == "EXAMPLE-1"
    # --issue is To Do, so the In Progress source is probed with a searched, re-read sample.
    assert checks[f"{in_progress}:source_status"]["status"] == "pass"
    assert checks[f"{in_progress}:transition_available"]["status"] == "pass"
    assert checks[f"{in_progress}:transition_available"]["details"]["sample_issue"] == "EXAMPLE-2"


@pytest.mark.parametrize("lie", ["search_status", "issue_status"])
def test_config_check_wrong_status_sample_is_unverified(harness: Harness, lie: str) -> None:
    setup_create(harness)
    if lie == "search_status":
        # The search returns a To Do issue for every status query.
        add_samples(harness, samples={})
        harness.server.routes[("GET", SEARCH)] = []
        harness.server.add("GET", SEARCH, {"issues": [_search_hit("EXAMPLE-1", "To Do")]})
    else:
        # The search claims In Progress, but the issue itself is still To Do.
        add_samples(harness, samples={"To Do": "EXAMPLE-1", "In Progress": "EXAMPLE-1"})
    result = harness.run("config", "check")
    assert result.code == 4
    assert result.error["code"] == "config_unverified"
    statuses = statuses_of(result)
    assert statuses["EXAMPLE:transition:done:In Progress"] == "unverified"
    assert not any(k.startswith("EXAMPLE:transition:done:In Progress:") for k in statuses)
    assert statuses["EXAMPLE:transition:in_progress:To Do:transition_available"] == "pass"
    assert f"{ISSUE_1}/transitions" in harness.server.paths()
    assert f"{ISSUE_2}/transitions" not in harness.server.paths()


def test_config_check_explicit_issue_in_other_status_falls_back_to_search(
    harness: Harness,
) -> None:
    setup_config_check(harness)
    result = harness.run("config", "check", "--issue", "EXAMPLE-2")
    assert result.code == 0, result.out
    checks = {c["id"]: c for c in result.data["checks"]}
    sample = checks["EXAMPLE:transition:in_progress:To Do:transition_available"]["details"]
    assert sample["sample_issue"] == "EXAMPLE-1"


@pytest.mark.parametrize(
    "damage", ["missing_fields", "null_fields", "list_fields", "entry_not_object", "no_required"]
)
def test_config_check_malformed_transition_metadata_is_unverified(
    harness: Harness, damage: str
) -> None:
    setup_config_check(harness)
    body = fixture("transitions_todo.json")
    start = body["transitions"][0]
    if damage == "missing_fields":
        del start["fields"]
    elif damage == "null_fields":
        start["fields"] = None
    elif damage == "list_fields":
        start["fields"] = []
    elif damage == "entry_not_object":
        start["fields"]["customfield_10300"] = "Work Started"
    else:
        del start["fields"]["customfield_10300"]["required"]
    harness.server.routes[("GET", f"{ISSUE_1}/transitions")] = []
    harness.server.add("GET", f"{ISSUE_1}/transitions", body)
    result = harness.run("config", "check")
    assert result.code == 4
    statuses = statuses_of(result)
    assert statuses["EXAMPLE:transition:in_progress:To Do"] == "unverified"
    assert "pass" not in {
        v for k, v in statuses.items() if k.startswith("EXAMPLE:transition:in_progress:")
    }


def test_config_check_duplicate_issue_type_names_are_drift(harness: Harness) -> None:
    setup_config_check(harness)
    types = fixture("createmeta_issuetypes.json")
    types["values"].append({"id": "10009", "name": "Story", "subtask": False})
    types["total"] = len(types["values"])
    harness.server.routes[("GET", ISSUETYPES)] = []
    harness.server.add("GET", ISSUETYPES, types)
    result = harness.run("config", "check")
    assert result.error["code"] == "config_drift"
    check = {c["id"]: c for c in result.error["details"]["checks"]}["EXAMPLE:Story:create_fields"]
    assert check["status"] == "drift"
    assert check["details"]["issue_type_ids"] == ["10001", "10009"]
    assert STORY_FIELDS not in harness.server.paths()


def _pre_edit_config(work_started: str = "Yes") -> str:
    return config_text(pre_edit="true", work_started=work_started)


def setup_pre_edit_check(harness: Harness, editmeta: Any, work_started: str = "Yes") -> None:
    setup_config_check(harness)
    harness.write_config(_pre_edit_config(work_started))
    harness.server.routes[("GET", f"{ISSUE_1}/transitions")] = []
    harness.server.add("GET", f"{ISSUE_1}/transitions", fixture("transitions_todo_no_screen.json"))
    harness.server.add("GET", f"{ISSUE_1}/editmeta", editmeta)


def test_config_check_pre_edit_off_screen_field_passes(harness: Harness) -> None:
    setup_pre_edit_check(harness, fixture("editmeta.json"))
    result = harness.run("config", "check")
    assert result.code == 0, result.out
    checks = {c["id"]: c for c in result.data["checks"]}
    check = checks["EXAMPLE:transition:in_progress:To Do:auto_set:work_started"]
    assert check["status"] == "pass"
    assert check["details"]["placement"] == "edit"
    assert harness.server.writes() == []


@pytest.mark.parametrize("damage", ["not_in_editmeta", "no_set_operation", "disallowed_value"])
def test_config_check_pre_edit_off_screen_field_drift(harness: Harness, damage: str) -> None:
    meta = fixture("editmeta.json")
    work_started = "Yes"
    if damage == "not_in_editmeta":
        del meta["fields"]["customfield_10300"]
    elif damage == "no_set_operation":
        meta["fields"]["customfield_10300"]["operations"] = ["add"]
    else:
        work_started = "Maybe"
    setup_pre_edit_check(harness, meta, work_started)
    result = harness.run("config", "check")
    assert result.code == 4
    assert result.error["code"] == "config_drift"
    check_id = "EXAMPLE:transition:in_progress:To Do:auto_set:work_started"
    assert statuses_of(result)[check_id] == "drift"
    assert harness.server.writes() == []


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
    harness.write_config()
    harness.store()
    harness.server.add("GET", ISSUETYPES, fixture("createmeta_issuetypes.json"))
    harness.server.add("GET", STORY_FIELDS, fixture("createmeta_story_fields.json"))
    harness.server.add("GET", FEATURE_FIELDS, fixture("createmeta_feature_fields.json"))
    add_samples(harness)
    result = harness.run("config", "check")
    assert result.code == 0, result.out
    assert result.data["status"] == "pass"
    statuses = statuses_of(result)
    assert statuses["EXAMPLE:transition:in_progress:To Do:transition_available"] == "pass"
    assert statuses["EXAMPLE:transition:done:In Progress:transition_available"] == "pass"
    assert statuses["EXAMPLE:transition:done:In Progress:source_status"] == "pass"


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


def _config_check_ids(result: Any) -> dict[str, dict[str, Any]]:
    checks = result.data["checks"] if result.code == 0 else result.error["details"]["checks"]
    return {c["id"]: c for c in checks}


def test_config_check_story_required_is_not_a_feature_requirement(harness: Harness) -> None:
    setup_config_check(harness)
    harness.write_config(team_config(story="[team]"))
    harness.server.routes[("GET", STORY_FIELDS)] = []
    harness.server.add("GET", STORY_FIELDS, with_team("createmeta_story_fields.json"))
    result = harness.run("config", "check")
    assert result.code == 0, result.out
    ids = _config_check_ids(result)
    assert ids["EXAMPLE:Story:field:team"]["status"] == "pass"
    assert ids["EXAMPLE:Story:field:acceptance_criteria"]["status"] == "pass"
    assert not any(k.startswith("EXAMPLE:Feature:field:") for k in ids)


def test_config_check_feature_required_off_screen_is_drift(harness: Harness) -> None:
    setup_config_check(harness)
    harness.write_config(team_config(feature="[team]"))
    result = harness.run("config", "check")
    assert result.code == 4
    assert result.error["code"] == "config_drift"
    ids = _config_check_ids(result)
    check = ids["EXAMPLE:Feature:field:team"]
    assert check["status"] == "drift"
    assert "not on the Feature create screen" in check["message"]
    assert "create.feature.required" in check["fix"]
    assert "EXAMPLE:Story:field:team" not in ids


@pytest.mark.parametrize(
    ("schema", "status"), [({"type": "string"}, "pass"), ({"type": "number"}, "drift")]
)
def test_config_check_feature_required_encoding(
    harness: Harness, schema: dict[str, Any], status: str
) -> None:
    setup_config_check(harness)
    harness.write_config(team_config(feature="[team]"))
    meta = with_team("createmeta_feature_fields.json")
    meta["values"][-1] = {**TEAM_FIELD, "schema": schema}
    harness.server.routes[("GET", FEATURE_FIELDS)] = []
    harness.server.add("GET", FEATURE_FIELDS, meta)
    result = harness.run("config", "check")
    assert _config_check_ids(result)["EXAMPLE:Feature:field:team"]["status"] == status


def test_config_check_explicit_issue_lookup_error_propagates(harness: Harness) -> None:
    setup_config_check(harness)
    harness.server.add(
        "GET", jira_path("/rest/api/2/issue/EXAMPLE-9"), {"errorMessages": ["nope"]}, status=404
    )
    result = harness.run("config", "check", "--issue", "EXAMPLE-9")
    assert result.code == 5
    assert result.error["code"] == "not_found"
    assert harness.server.writes() == []


def test_config_check_required_field_without_set_is_drift(harness: Harness) -> None:
    setup_config_check(harness)
    meta = fixture("createmeta_story_fields.json")
    for value in meta["values"]:
        if value["fieldId"] == "customfield_10100":
            value["operations"] = []
    harness.server.routes[("GET", STORY_FIELDS)] = []
    harness.server.add("GET", STORY_FIELDS, meta)
    result = harness.run("config", "check")
    assert result.code == 4
    assert result.error["code"] == "config_drift"
    check = _config_check_ids(result)["EXAMPLE:Story:field:acceptance_criteria"]
    assert check["status"] == "drift"
    assert "cannot be set" in check["message"]
    assert "fields.acceptance_criteria" in check["fix"]
    assert check["details"] == {"field_id": "customfield_10100", "operations": []}
    create = create_story(harness, STORY)
    assert create.code == 4
    assert checks(create.error)["field:acceptance_criteria"]["status"] == "fail"


def test_config_check_required_fields_sharing_an_id_is_drift(harness: Harness) -> None:
    setup_config_check(harness)
    harness.write_config(
        config_text().replace(
            "{jira_name: Parent Link, encoding: issue_key_string}",
            "{id: customfield_10100, encoding: issue_key_string}",
        )
    )
    result = harness.run("config", "check")
    assert result.code == 4
    assert result.error["code"] == "config_drift"
    check = _config_check_ids(result)["EXAMPLE:Story:field:feature_link"]
    assert check["status"] == "drift"
    assert check["details"] == {
        "friendly": ["acceptance_criteria", "feature_link"],
        "field_id": "customfield_10100",
    }


def _require_description(name: str) -> Any:
    meta = fixture(name)
    for value in meta["values"]:
        if value["fieldId"] == "description":
            value["required"] = True
    return meta


@pytest.mark.parametrize(
    ("type_name", "route", "meta_file"),
    [
        ("Story", STORY_FIELDS, "createmeta_story_fields.json"),
        ("Feature", FEATURE_FIELDS, "createmeta_feature_fields.json"),
    ],
)
def test_config_check_reports_required_description(
    harness: Harness, type_name: str, route: str, meta_file: str
) -> None:
    setup_config_check(harness)
    harness.server.routes[("GET", route)] = []
    harness.server.add("GET", route, _require_description(meta_file))
    result = harness.run("config", "check")
    assert result.code == 4
    assert result.error["code"] == "config_drift"
    ids = _config_check_ids(result)
    check = ids[f"EXAMPLE:{type_name}:create_field:description"]
    assert check["status"] == "drift"
    assert check["details"] == {"field_id": "description"}
    assert "top-level `description`" in check["fix"]
    assert "Jira admin" in check["fix"]
    other = "Feature" if type_name == "Story" else "Story"
    assert f"EXAMPLE:{other}:create_field:description" not in ids


def test_story_create_requires_description_when_jira_does(harness: Harness) -> None:
    setup_create(harness, story_fields=_require_description("createmeta_story_fields.json"))
    result = create_story(harness, STORY)
    assert result.code == 4
    check = checks(result.error)["create_field:description"]
    assert check["status"] == "fail"
    assert "top-level `description`" in check["fix"]
    assert harness.server.writes() == []

    assert create_story(harness, {**STORY, "description": "d"}).code == 0


ISSUE_3 = jira_path("/rest/api/2/issue/EXAMPLE-3")


def _issue_reply(*, key: str = "EXAMPLE-3", project: str = "EXAMPLE") -> dict[str, Any]:
    issue: dict[str, Any] = fixture("issue_todo.json")
    issue["key"] = key
    issue["fields"]["project"] = {"key": project, "id": "1"}
    return issue


@pytest.mark.parametrize(
    "reply", [{"key": "EXAMPLE-9"}, {"project": "OTHER"}, {"key": "OTHER-3", "project": "OTHER"}]
)
def test_config_check_explicit_issue_mismatch_uses_searched_sample(
    harness: Harness, reply: dict[str, str]
) -> None:
    setup_config_check(harness)
    harness.server.add("GET", ISSUE_3, _issue_reply(**reply))
    result = harness.run("config", "check", "--issue", "EXAMPLE-3")
    assert result.code == 0, result.out
    ids = _config_check_ids(result)
    for check_id, sample in (
        ("EXAMPLE:transition:in_progress:To Do:transition_available", "EXAMPLE-1"),
        ("EXAMPLE:transition:done:In Progress:transition_available", "EXAMPLE-2"),
    ):
        assert ids[check_id]["status"] == "pass"
        assert ids[check_id]["details"]["sample_issue"] == sample
    assert f"{ISSUE_3}/transitions" not in harness.server.paths()
    # Each searched sample is re-read before it is probed.
    assert harness.server.paths("GET").count(ISSUE_1) == 1
    assert harness.server.paths("GET").count(ISSUE_2) == 1


@pytest.mark.parametrize("reply", [{"key": "EXAMPLE-9"}, {"project": "OTHER"}])
def test_config_check_explicit_issue_mismatch_without_search_sample_is_unverified(
    harness: Harness, reply: dict[str, str]
) -> None:
    setup_config_check(harness, sample_found=False)
    harness.server.add("GET", ISSUE_3, _issue_reply(**reply))
    result = harness.run("config", "check", "--issue", "EXAMPLE-3")
    assert result.code == 4
    assert result.error["code"] == "config_unverified"
    statuses = statuses_of(result)
    assert statuses["EXAMPLE:transition:in_progress:To Do"] == "unverified"
    assert statuses["EXAMPLE:transition:done:In Progress"] == "unverified"
    assert not any(":transition_available" in k for k in statuses)
    assert not any(p.endswith("/transitions") for p in harness.server.paths())


@pytest.mark.parametrize("reread", [{"key": "EXAMPLE-9"}, {"project": "OTHER"}])
def test_config_check_searched_sample_failing_reread_is_unverified(
    harness: Harness, reread: dict[str, str]
) -> None:
    setup_config_check(harness)
    harness.server.add("GET", ISSUE_3, _issue_reply(project="OTHER"))
    harness.server.routes[("GET", ISSUE_1)] = []
    harness.server.add("GET", ISSUE_1, _issue_reply(**{"key": "EXAMPLE-1", **reread}))
    result = harness.run("config", "check", "--issue", "EXAMPLE-3")
    assert result.code == 4
    assert result.error["code"] == "config_unverified"
    ids = _config_check_ids(result)
    todo = ids["EXAMPLE:transition:in_progress:To Do"]
    assert todo["status"] == "unverified"
    assert todo["details"]["sample_issue"] == "EXAMPLE-1"
    assert f"{ISSUE_1}/transitions" not in harness.server.paths()
    assert ids["EXAMPLE:transition:done:In Progress:transition_available"]["status"] == "pass"


# --------------------------------------------------------------------------- review regressions


def _client(server: FakeServer) -> ServiceClient:
    return ServiceClient(
        "jira", JIRA, Credential(auth="pat", secret=JIRA_TOKEN), transport=server.transport()
    )


def _jira_config(tmp_path: Path, **kwargs: Any) -> JiraConfig:
    path = tmp_path / "atl.yaml"
    path.write_text(config_text(**kwargs))
    jira_config = parse_config_file(path).jira
    assert jira_config is not None
    return jira_config


def _feature(number: int, summary: str) -> dict[str, Any]:
    return {
        "id": str(number),
        "key": f"EXAMPLE-{number}",
        "fields": {"summary": summary, "project": {"key": "EXAMPLE"}, "status": {"name": "Open"}},
    }


def _feature_search(
    text_hits: list[dict[str, Any]],
    named_hits: list[dict[str, Any]],
    *,
    page_cap: int | None = None,
    send_total: bool = True,
) -> Any:
    """Serve feature-find searches: `key in` queries get named hits, others get text hits."""

    def respond(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        hits = named_hits if "key in (" in params["jql"] else text_hits
        start, size = int(params["startAt"]), int(params["maxResults"])
        size = min(size, page_cap) if page_cap else size
        body: dict[str, Any] = {"startAt": start, "maxResults": size}
        body["issues"] = hits[start : start + size]
        if send_total:
            body["total"] = len(hits)
        return httpx.Response(200, json=body)

    return respond


def _search_requests(harness: Harness) -> list[httpx.QueryParams]:
    return [r.url.params for r in harness.server.requests if r.url.path == SEARCH]


def test_feature_find_named_key_survives_full_text_pool(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    text_hits = [_feature(1000 + n, f"login widget {n}") for n in range(250)]
    named = _feature(5, "Legacy login portal")
    text_hits.insert(200, named)
    harness.server.add("GET", SEARCH, handler=_feature_search(text_hits, [named]))
    data = harness.run("jira", "feature", "find", "--text", "login EXAMPLE-5").data
    keys = [c["key"] for c in data["candidates"]]
    assert keys[0] == "EXAMPLE-5"
    assert data["candidates"][0]["score"] == 110
    assert len(keys) == len(set(keys)) == 10
    assert data["candidate_pool_limit"] == CANDIDATE_POOL == 100
    assert data["truncated"] is True
    named_query, text_query = _search_requests(harness)
    assert 'key in ("EXAMPLE-5")' in named_query["jql"]
    assert 'summary ~ "login"' not in named_query["jql"]
    assert 'summary ~ "login"' in text_query["jql"]
    assert "key in" not in text_query["jql"]
    assert text_query["jql"].endswith("ORDER BY updated DESC")
    assert text_query["maxResults"] == "100"
    assert harness.server.writes() == []


def test_feature_find_named_keys_page_past_server_cap(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    named = [_feature(5, "Alpha"), _feature(6, "Beta")]
    search = _feature_search([], named, page_cap=1)
    harness.server.add("GET", SEARCH, handler=search)
    data = harness.run("jira", "feature", "find", "--text", "EXAMPLE-5 EXAMPLE-6 EXAMPLE-7").data
    assert [c["key"] for c in data["candidates"]] == ["EXAMPLE-5", "EXAMPLE-6"]
    assert data["truncated"] is False
    named_starts = [q["startAt"] for q in _search_requests(harness) if "key in (" in q["jql"]]
    assert named_starts == ["0", "1"]


@pytest.mark.parametrize(
    "named_hits",
    [
        [_feature(5, "Alpha"), _feature(5, "Alpha")],
        [_feature(5, "Alpha"), {"key": "EXAMPLE-6"}],
        [_feature(5, "Alpha"), {"key": "EXAMPLE-99", "fields": {}}],
    ],
    ids=["duplicate", "missing-fields", "unexpected-key"],
)
def test_feature_find_rejects_invalid_named_results(
    harness: Harness, named_hits: list[dict[str, Any]]
) -> None:
    harness.write_config()
    harness.store()
    harness.server.add("GET", SEARCH, handler=_feature_search([], named_hits, page_cap=1))
    result = harness.run("jira", "feature", "find", "--text", "EXAMPLE-5 EXAMPLE-6")
    assert result.code == 6
    assert result.error["code"] == "response_invalid"
    assert harness.server.writes() == []


def test_feature_find_limit_alone_is_not_truncation(harness: Harness) -> None:
    setup_find(harness)
    data = harness.run("jira", "feature", "find", "--text", "login sign", "--limit", "1").data
    assert len(data["candidates"]) == 1
    assert data["truncated"] is False


@pytest.mark.parametrize(("hits", "truncated"), [(100, True), (99, False)])
def test_feature_find_truncation_without_total(
    harness: Harness, hits: int, truncated: bool
) -> None:
    harness.write_config()
    harness.store()
    text_hits = [_feature(n, f"login {n}") for n in range(1, hits + 1)]
    harness.server.add("GET", SEARCH, handler=_feature_search(text_hits, [], send_total=False))
    assert harness.run("jira", "feature", "find", "--text", "login").data["truncated"] is truncated


@pytest.mark.parametrize("project", ["EXAMPLE", ["EXAMPLE"], {"key": 7}, None])
@pytest.mark.parametrize("status", ["Open", ["Open"], {"name": 1}, None])
def test_rank_tolerates_malformed_nested_values(project: Any, status: Any) -> None:
    issues = [
        {"key": "EXAMPLE-5", "fields": {"summary": "login", "project": project, "status": status}}
    ]
    (candidate,) = rank(issues, ["login"], set(), "EXAMPLE", None, 10)
    assert candidate.project == "EXAMPLE"
    assert candidate.status is None
    assert candidate.score == 15


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


@pytest.mark.parametrize("fields", ["missing", None, "x", []])
def test_get_issue_malformed_fields_is_response_invalid(harness: Harness, fields: Any) -> None:
    harness.write_config()
    harness.store()
    issue = fixture("issue_todo.json")
    if fields == "missing":
        del issue["fields"]
    else:
        issue["fields"] = fields
    harness.server.add("GET", jira_path("/rest/api/2/issue/EXAMPLE-1"), issue)
    result = harness.run("jira", "feature", "find", "--issue", "EXAMPLE-1")
    assert result.code == 6
    assert result.error["code"] == "response_invalid"
    assert harness.server.paths() == [jira_path("/rest/api/2/issue/EXAMPLE-1")]


def test_story_create_rejects_feature_key_with_trailing_newline(harness: Harness) -> None:
    setup_create(harness)
    result = create_story(harness, {**STORY, "feature_key": "EXAMPLE-10\n"})
    assert result.code == 4
    assert checks(result.error)["feature_key"]["status"] == "fail"
    assert FEATURE_ISSUE not in harness.server.paths()
    assert harness.server.writes() == []


def test_create_response_key_with_trailing_newline_is_unknown(harness: Harness) -> None:
    setup_create(harness)
    harness.server.routes[("POST", CREATE)] = []
    harness.server.add("POST", CREATE, {"id": "30001", "key": "EXAMPLE-42\n"}, status=201)
    result = create_story(harness, STORY)
    assert result.code == 6
    assert result.error["details"]["reason"] == "create response did not include a valid issue key"


def test_feature_create_domain_gate_cannot_be_bypassed(tmp_path: Path) -> None:
    project = _jira_config(tmp_path).projects["EXAMPLE"]
    server = FakeServer()
    with pytest.raises(AtlError) as info:
        feature_create(_client(server), project, {"project": "EXAMPLE", "summary": "SSO"})
    assert info.value.code == "approval_required"
    assert info.value.exit_code == 4
    assert server.requests == []


def test_feature_create_cli_uses_domain_approval_rule(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_create(harness)
    calls: list[dict[str, Any]] = []
    real = jira_module.require_feature_confirmation

    def spy(project: Any, doc: dict[str, Any]) -> None:
        calls.append(doc)
        real(project, doc)

    monkeypatch.setattr(jira_module, "require_feature_confirmation", spy)
    result = create_feature(harness, {"project": "EXAMPLE", "summary": "SSO"})
    assert result.error["code"] == "approval_required"
    assert result.code == 4
    assert calls == [{"project": "EXAMPLE", "summary": "SSO"}]
    assert harness.server.requests == []


def test_feature_create_missing_approval_precedes_missing_credentials(harness: Harness) -> None:
    harness.write_config(config_text(policy="human_confirmed"))
    result = create_feature(harness, {"project": "EXAMPLE", "summary": "SSO"})
    assert result.code == 4
    assert result.error["code"] == "approval_required"
    assert harness.server.requests == []


def test_createmeta_empty_final_page_without_markers(harness: Harness) -> None:
    meta = fixture("createmeta_story_fields.json")
    unmarked = {k: v for k, v in meta.items() if k not in ("isLast", "total")}
    setup_create(harness)
    harness.server.routes[("GET", STORY_FIELDS)] = []
    end = len(meta["values"])
    harness.server.add("GET", STORY_FIELDS, {**unmarked, "values": meta["values"][:4]})
    harness.server.add(
        "GET", STORY_FIELDS, {**unmarked, "startAt": 4, "values": meta["values"][4:]}
    )
    harness.server.add("GET", STORY_FIELDS, {**unmarked, "startAt": end, "values": []})
    assert create_story(harness, STORY).code == 0
    starts = [
        r.url.params["startAt"] for r in harness.server.requests if r.url.path == STORY_FIELDS
    ]
    assert starts == ["0", "4", str(end)]


@pytest.mark.parametrize("markers", [{"isLast": False}, {"total": 99}])
def test_createmeta_empty_page_with_markers_still_fails_closed(
    harness: Harness, markers: dict[str, Any]
) -> None:
    meta = fixture("createmeta_story_fields.json")
    unmarked = {k: v for k, v in meta.items() if k not in ("isLast", "total")}
    setup_create(harness)
    harness.server.routes[("GET", STORY_FIELDS)] = []
    harness.server.add("GET", STORY_FIELDS, {**unmarked, "values": meta["values"][:4]})
    harness.server.add("GET", STORY_FIELDS, {**unmarked, **markers, "startAt": 4, "values": []})
    result = create_story(harness, STORY)
    assert result.error["code"] == "metadata_incomplete"
    assert harness.server.writes() == []


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
    (harness.tmp_path / "f.txt").write_bytes(b"x")

    def broken(fd: int) -> os.stat_result:
        raise OSError("gone")

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
    (request,) = harness.server.requests
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
    (request,) = harness.server.requests
    assert b"\r\n\r\nxxxxx\r\n--" in request.content
    assert path.read_bytes() == b"yyyyy"


@pytest.mark.parametrize("first_markers", [{"total": 99}, {"total": 99, "isLast": False}])
def test_createmeta_earlier_total_bounds_unmarked_empty_page(
    harness: Harness, first_markers: dict[str, Any]
) -> None:
    meta = fixture("createmeta_story_fields.json")
    unmarked = {k: v for k, v in meta.items() if k not in ("isLast", "total")}
    setup_create(harness)
    harness.server.routes[("GET", STORY_FIELDS)] = []
    harness.server.add(
        "GET", STORY_FIELDS, {**unmarked, **first_markers, "values": meta["values"][:4]}
    )
    harness.server.add("GET", STORY_FIELDS, {**unmarked, "startAt": 4, "values": []})
    result = create_story(harness, STORY)
    assert result.code == 6
    assert result.error["code"] == "metadata_incomplete"
    assert result.error["details"]["total"] == 99
    assert harness.server.writes() == []


def test_createmeta_earlier_total_ends_unmarked_pages(harness: Harness) -> None:
    meta = fixture("createmeta_story_fields.json")
    unmarked = {k: v for k, v in meta.items() if k not in ("isLast", "total")}
    end = len(meta["values"])
    setup_create(harness)
    harness.server.routes[("GET", STORY_FIELDS)] = []
    harness.server.add(
        "GET", STORY_FIELDS, {**unmarked, "total": end, "values": meta["values"][:4]}
    )
    harness.server.add(
        "GET", STORY_FIELDS, {**unmarked, "startAt": 4, "values": meta["values"][4:]}
    )
    assert create_story(harness, STORY).code == 0
    assert harness.server.paths("GET").count(STORY_FIELDS) == 2


def test_createmeta_changed_total_fails_closed(harness: Harness) -> None:
    meta = fixture("createmeta_story_fields.json")
    end = len(meta["values"])
    setup_create(harness)
    harness.server.routes[("GET", STORY_FIELDS)] = []
    first = {**meta, "values": meta["values"][:4], "isLast": False, "total": end + 1}
    harness.server.add("GET", STORY_FIELDS, first)
    harness.server.add(
        "GET",
        STORY_FIELDS,
        {**meta, "values": meta["values"][4:], "isLast": True, "total": end, "startAt": 4},
    )
    result = create_story(harness, STORY)
    assert result.error["code"] == "metadata_incomplete"
    assert result.error["details"]["previous_total"] == end + 1
    assert harness.server.writes() == []


def _text_starts(harness: Harness) -> list[str]:
    return [q["startAt"] for q in _search_requests(harness) if "key in (" not in q["jql"]]


@pytest.mark.parametrize(
    ("hits", "send_total", "starts", "truncated"),
    [
        (150, True, ["0", "50"], True),
        (80, True, ["0", "50"], False),
        (150, False, ["0", "50"], True),
        (80, False, ["0", "50", "80"], False),
    ],
    ids=["over-pool-total", "under-pool-total", "over-pool-no-total", "under-pool-no-total"],
)
def test_feature_find_text_pool_pages_past_server_cap(
    harness: Harness, hits: int, send_total: bool, starts: list[str], truncated: bool
) -> None:
    harness.write_config()
    harness.store()
    text_hits = [_feature(n, f"login {n}") for n in range(1, hits + 1)]
    search = _feature_search(text_hits, [], page_cap=50, send_total=send_total)
    harness.server.add("GET", SEARCH, handler=search)
    data = harness.run("jira", "feature", "find", "--text", "login", "--limit", "25").data
    assert _text_starts(harness) == starts
    assert data["truncated"] is truncated
    # Ties rank by key number, so the lowest-numbered pooled hit leads.
    assert data["candidates"][0]["key"] == "EXAMPLE-1"
    assert len(data["candidates"]) == 25
    requested = [int(q["maxResults"]) for q in _search_requests(harness)]
    assert requested == [CANDIDATE_POOL - int(start) for start in starts]


def test_feature_find_text_pool_empty_page_short_of_total_is_truncated(harness: Harness) -> None:
    harness.write_config()
    harness.store()

    def respond(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["startAt"])
        issues = [_feature(1, "login"), _feature(2, "login")] if start == 0 else []
        return httpx.Response(200, json={"startAt": start, "total": 5, "issues": issues})

    harness.server.add("GET", SEARCH, handler=respond)
    data = harness.run("jira", "feature", "find", "--text", "login").data
    assert [c["key"] for c in data["candidates"]] == ["EXAMPLE-1", "EXAMPLE-2"]
    assert data["truncated"] is True
    assert _text_starts(harness) == ["0", "2"]


def test_feature_find_named_empty_page_short_of_total_is_response_invalid(
    harness: Harness,
) -> None:
    harness.write_config()
    harness.store()

    def respond(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["startAt"])
        issues = [_feature(5, "Alpha")] if start == 0 else []
        return httpx.Response(200, json={"startAt": start, "total": 2, "issues": issues})

    harness.server.add("GET", SEARCH, handler=respond)
    result = harness.run("jira", "feature", "find", "--text", "EXAMPLE-5 EXAMPLE-6")
    assert result.code == 6
    assert result.error["code"] == "response_invalid"
    assert result.error["message"] == "Jira returned an invalid named feature result."
    assert harness.server.writes() == []


def test_feature_find_named_keys_page_without_total(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    named = [_feature(5, "Alpha"), _feature(6, "Beta")]
    harness.server.add(
        "GET", SEARCH, handler=_feature_search([], named, page_cap=1, send_total=False)
    )
    data = harness.run("jira", "feature", "find", "--text", "EXAMPLE-5 EXAMPLE-6 EXAMPLE-7").data
    assert [c["key"] for c in data["candidates"]] == ["EXAMPLE-5", "EXAMPLE-6"]
    named_starts = [q["startAt"] for q in _search_requests(harness) if "key in (" in q["jql"]]
    assert named_starts == ["0", "1", "2"]


def test_feature_find_overlapping_text_and_named_hit_ranks_once(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    overlap = _feature(5, "Legacy login portal")
    text_hits = [overlap, _feature(6, "login widget")]
    harness.server.add("GET", SEARCH, handler=_feature_search(text_hits, [overlap]))
    data = harness.run("jira", "feature", "find", "--text", "login EXAMPLE-5").data
    assert [(c["key"], c["score"]) for c in data["candidates"]] == [
        ("EXAMPLE-5", WEIGHT_EXACT_KEY + WEIGHT_TOKEN),
        ("EXAMPLE-6", WEIGHT_TOKEN),
    ]


def test_feature_find_named_jql_is_scoped_to_configured_feature_type(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    feature = _feature(5, "Alpha")
    story = _feature(7, "Story mentioned by key")

    def respond(request: httpx.Request) -> httpx.Response:
        jql = request.url.params["jql"]
        # Simulate Jira applying the JQL scope: a story is only excluded by the type clause.
        issues = [feature] if 'issuetype = "Feature"' in jql else [feature, story]
        return httpx.Response(200, json={"startAt": 0, "total": len(issues), "issues": issues})

    harness.server.add("GET", SEARCH, handler=respond)
    data = harness.run("jira", "feature", "find", "--text", "EXAMPLE-5 EXAMPLE-7").data
    assert [c["key"] for c in data["candidates"]] == ["EXAMPLE-5"]
    (named_query,) = [q for q in _search_requests(harness) if "key in (" in q["jql"]]
    assert named_query["jql"].startswith(
        '((project = "EXAMPLE" AND issuetype = "Feature")) AND key in ("EXAMPLE-5", "EXAMPLE-7")'
    )


def test_search_raw_empty_page_ends_despite_advertised_total(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    harness.server.add("GET", SEARCH, {"startAt": 20, "total": 500, "issues": []})
    result = harness.run("jira", "search", "--jql", "x", "--start", "20", "--limit", "10")
    assert result.code == 0
    assert result.data["issues"] == []
    assert result.data["next_start"] is None
