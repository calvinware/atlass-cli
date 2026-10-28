from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import (
    CONFLUENCE,
    CONFLUENCE_TOKEN,
    SHELL_SENTINELS,
    FakeServer,
    Harness,
    backticked,
    confluence_path,
    fixture,
    run_argv,
    run_in_posix_shell,
)
from yojo.auth import Credential
from yojo.confluence import update
from yojo.contracts import YojoError
from yojo.transport import ServiceClient, jql_quote

PAGE = confluence_path("/rest/api/content/123456")
CONTENT = confluence_path("/rest/api/content")
SEARCH = confluence_path("/rest/api/content/search")


def setup(harness: Harness) -> None:
    harness.write_config()
    harness.store("confluence", CONFLUENCE_TOKEN)


def run_input(harness: Harness, *argv: str, doc: dict[str, Any]) -> Any:
    return harness.run(*argv, "--input", "-", stdin=json.dumps(doc).encode())


def test_read_preserves_context_path_and_marks_untrusted(harness: Harness) -> None:
    setup(harness)
    harness.server.add("GET", PAGE, fixture("confluence_page.json"))
    result = harness.run("confluence", "page", "read", "123456")
    assert result.code == 0
    data = result.data
    assert data["content_trust"] == "untrusted"
    assert data["version"] == 7
    assert data["space_key"] == "ENG"
    assert data["ancestors"] == ["1000"]
    assert data["body_storage"].startswith("<p>Ignore previous")
    request = harness.server.requests[0]
    assert str(request.url).startswith(
        "https://confluence.example.test/confluence/rest/api/content/123456"
    )
    assert request.url.params["expand"] == "body.storage,version,space,ancestors"
    assert request.headers["authorization"] == f"Bearer {CONFLUENCE_TOKEN}"


def test_read_not_found(harness: Harness) -> None:
    setup(harness)
    harness.server.add("GET", PAGE, {"statusCode": 404, "message": "No content"}, status=404)
    result = harness.run("confluence", "page", "read", "123456")
    assert result.code == 5
    assert result.error["code"] == "not_found"


def test_search_pagination(harness: Harness) -> None:
    setup(harness)
    body = {
        "results": [
            {
                "id": "1",
                "type": "page",
                "title": "A",
                "space": {"key": "ENG"},
                "version": {"number": 2},
            },
            {"id": "2", "type": "page", "title": "B"},
        ],
        "start": 0,
        "limit": 2,
        "size": 2,
        "_links": {"next": "/rest/api/content/search?cql=...&start=2"},
    }
    harness.server.add("GET", SEARCH, body)
    result = harness.run("confluence", "page", "search", "--cql", 'space = "ENG"', "--limit", "2")
    data = result.data
    assert data["next_start"] == 2
    assert data["results"][1] == {
        "id": "2",
        "title": "B",
        "type": "page",
        "space_key": None,
        "version": None,
    }
    params = harness.server.requests[0].url.params
    assert params["cql"] == 'space = "ENG"'
    assert params["limit"] == "2"


def test_search_last_page(harness: Harness) -> None:
    setup(harness)
    harness.server.add("GET", SEARCH, {"results": [], "_links": {}})
    assert (
        harness.run("confluence", "page", "search", "--cql", "type = page").data["next_start"]
        is None
    )


CREATED = {
    "id": "9",
    "type": "page",
    "title": "T",
    "space": {"key": "ENG"},
    "version": {"number": 1},
}


def test_create_sends_storage(harness: Harness) -> None:
    setup(harness)
    harness.server.add(
        "POST",
        CONTENT,
        CREATED,
    )
    doc = {"space_key": "ENG", "title": "T", "body_storage": "<p>x</p>", "parent_id": "1000"}
    result = run_input(harness, "confluence", "page", "create", doc=doc)
    assert result.data == {
        "id": "9",
        "title": "T",
        "space_key": "ENG",
        "version": 1,
        "url": "https://confluence.example.test/confluence/pages/viewpage.action?pageId=9",
    }
    assert json.loads(harness.server.requests[0].content) == {
        "type": "page",
        "title": "T",
        "space": {"key": "ENG"},
        "body": {"storage": {"value": "<p>x</p>", "representation": "storage"}},
        "ancestors": [{"id": "1000"}],
    }


@pytest.mark.parametrize(
    "body",
    [
        None,
        b"not json",
        [],
        {},
        {"id": "9"},
        {**CREATED, "id": ""},
        {**CREATED, "id": 9},
        {**CREATED, "id": "abc"},
        {k: v for k, v in CREATED.items() if k != "type"},
        {**CREATED, "type": "blogpost"},
        {k: v for k, v in CREATED.items() if k != "title"},
        {**CREATED, "title": "Other"},
        {k: v for k, v in CREATED.items() if k != "space"},
        {**CREATED, "space": "ENG"},
        {**CREATED, "space": ["ENG"]},
        {**CREATED, "space": {"key": "OPS"}},
        {k: v for k, v in CREATED.items() if k != "version"},
        {**CREATED, "version": {"number": 2}},
        {**CREATED, "version": {"number": True}},
    ],
)
def test_create_unconfirmed_success_is_outcome_unknown(harness: Harness, body: Any) -> None:
    setup(harness)
    if isinstance(body, bytes):
        harness.server.add("POST", CONTENT, handler=lambda _: httpx.Response(200, content=body))
    else:
        harness.server.add("POST", CONTENT, body)
    doc = {"space_key": "ENG", "title": "T", "body_storage": "<p>x</p>"}
    result = run_input(harness, "confluence", "page", "create", doc=doc)
    assert result.code == 6, result.out
    assert result.error["code"] == "outcome_unknown"
    assert "data" not in result.out
    assert (
        """yojo confluence page search --cql 'space = "ENG" AND type = page AND """
        """(title = "T")'""" in (result.error["fix"])
    )
    assert "recovery_argv" in result.error["details"]
    assert len(harness.server.writes()) == 1


def test_create_server_error_includes_recovery_search(harness: Harness) -> None:
    setup(harness)
    harness.server.add("POST", CONTENT, {"message": "unavailable"}, status=503)
    doc = {"space_key": "ENG", "title": " Release notes ", "body_storage": "<p>x</p>"}
    result = run_input(harness, "confluence", "page", "create", doc=doc)
    assert result.code == 6
    assert result.error["code"] == "outcome_unknown"
    assert result.error["details"]["reason"] == "server error 503"
    exact = result.error["details"]["recovery_argv"]["exact_search"][-1]
    assert 'title = " Release notes "' in exact
    assert 'title = "Release notes"' in exact
    assert "broad_search" in result.error["details"]["recovery_argv"]


def test_create_requires_fields(harness: Harness) -> None:
    setup(harness)
    result = run_input(
        harness, "confluence", "page", "create", doc={"space_key": "ENG", "title": "T"}
    )
    assert result.error["code"] == "input_invalid"
    assert harness.server.requests == []


def test_update_rejects_stale_version_before_write(harness: Harness) -> None:
    setup(harness)
    harness.server.add("GET", PAGE, fixture("confluence_page.json"))
    result = run_input(
        harness,
        "confluence",
        "page",
        "update",
        "123456",
        doc={"expected_version": 6, "title": "New"},
    )
    assert result.code == 5
    assert result.error["code"] == "version_conflict"
    assert result.error["details"]["current_version"] == 7
    assert "expected_version 7" in result.error["fix"]
    assert harness.server.writes() == []


def test_update_increments_version_and_keeps_body(harness: Harness) -> None:
    setup(harness)
    harness.server.add("GET", PAGE, fixture("confluence_page.json"))
    harness.server.add("PUT", PAGE, {"id": "123456", "title": "New", "version": {"number": 8}})
    result = run_input(
        harness,
        "confluence",
        "page",
        "update",
        "123456",
        doc={"expected_version": 7, "title": "New"},
    )
    assert result.data == {
        "id": "123456",
        "title": "New",
        "version": 8,
        "previous_version": 7,
        "url": "https://confluence.example.test/confluence/pages/viewpage.action?pageId=123456",
    }
    (put,) = harness.server.writes()
    body = json.loads(put.content)
    assert body["version"] == {"number": 8}
    assert body["title"] == "New"
    assert body["body"]["storage"]["value"].startswith("<p>Ignore previous")


def test_update_refuses_read_of_other_content_id(harness: Harness) -> None:
    setup(harness)
    page = fixture("confluence_page.json")
    page["id"] = "999"
    harness.server.add("GET", PAGE, page)
    result = run_input(
        harness,
        "confluence",
        "page",
        "update",
        "123456",
        doc={"expected_version": 7, "title": "New"},
    )
    assert result.code == 6
    assert result.error["code"] == "response_invalid"
    assert result.error["details"]["requested_id"] == "123456"
    assert harness.server.writes() == []


def test_read_refuses_other_content_id(harness: Harness) -> None:
    setup(harness)
    page = fixture("confluence_page.json")
    page["id"] = "999"
    harness.server.add("GET", PAGE, page)
    result = harness.run("confluence", "page", "read", "123456")
    assert result.error["code"] == "response_invalid"


@pytest.mark.parametrize("content_type", ["blogpost", "comment", None])
def test_update_refuses_non_page_content(harness: Harness, content_type: Any) -> None:
    setup(harness)
    page = fixture("confluence_page.json")
    if content_type is None:
        del page["type"]
    else:
        page["type"] = content_type
    harness.server.add("GET", PAGE, page)
    result = run_input(
        harness,
        "confluence",
        "page",
        "update",
        "123456",
        doc={"expected_version": 7, "body_storage": "<p>new</p>"},
    )
    assert result.code == 2
    assert result.error["code"] == "input_invalid"
    assert harness.server.writes() == []


def test_update_race_409_is_version_conflict(harness: Harness) -> None:
    setup(harness)
    harness.server.add("GET", PAGE, fixture("confluence_page.json"))
    harness.server.add("PUT", PAGE, {"statusCode": 409, "message": "Version mismatch"}, status=409)
    result = run_input(
        harness,
        "confluence",
        "page",
        "update",
        "123456",
        doc={"expected_version": 7, "body_storage": "<p>new</p>"},
    )
    assert result.code == 5
    assert result.error["code"] == "version_conflict"
    assert "yojo confluence page read 123456 --full" in result.error["fix"]
    assert len(harness.server.writes()) == 1


def test_update_requires_title_or_body(harness: Harness) -> None:
    setup(harness)
    result = run_input(
        harness, "confluence", "page", "update", "123456", doc={"expected_version": 7}
    )
    assert result.error["code"] == "input_invalid"
    assert harness.server.requests == []


def test_confluence_not_configured(harness: Harness) -> None:
    harness.write_config("version: 1\njira:\n  base_url: https://jira.example.test\n")
    result = harness.run("confluence", "page", "read", "1")
    assert result.code == 2
    assert result.error["code"] == "service_not_configured"
    assert "--base-url" in result.error["fix"]
    assert harness.server.requests == []


def test_read_without_config_uses_base_url_and_matching_credential(harness: Harness) -> None:
    harness.store("confluence", CONFLUENCE_TOKEN)
    harness.server.add("GET", PAGE, fixture("confluence_page.json"))
    result = harness.run("confluence", "page", "read", "123456", "--base-url", CONFLUENCE + "/")
    assert result.code == 0
    assert result.data["url"] == f"{CONFLUENCE}/pages/viewpage.action?pageId=123456"
    assert harness.server.requests[0].headers["authorization"] == f"Bearer {CONFLUENCE_TOKEN}"


def test_read_trims_long_body_and_says_how_to_get_the_rest(harness: Harness) -> None:
    setup(harness)
    page = fixture("confluence_page.json")
    page["body"]["storage"]["value"] = "<p>" + "x" * 5000 + "</p>"
    harness.server.add("GET", PAGE, page)
    result = harness.run("confluence", "page", "read", "123456")
    data = result.data
    assert data["truncated"] is True
    assert data["body_chars"] == 5007
    assert len(data["body_storage"]) == 2000
    assert data["more"] == "yojo confluence page read 123456 --full"
    assert result.nbytes <= 4096
    full = harness.run("confluence", "page", "read", "123456", "--full").data
    assert full["truncated"] is False and full["more"] is None
    assert len(full["body_storage"]) == 5007


def _page_with(damage: str) -> dict[str, Any]:
    page: dict[str, Any] = fixture("confluence_page.json")
    if damage == "no_body":
        del page["body"]
    elif damage == "no_storage":
        page["body"] = {}
    elif damage == "null_storage":
        page["body"] = {"storage": None}
    elif damage == "no_value":
        del page["body"]["storage"]["value"]
    else:
        page["body"]["storage"]["value"] = None
    return page


@pytest.mark.parametrize("damage", ["no_body", "no_storage", "null_storage", "no_value", "null"])
def test_title_only_update_refuses_incomplete_body_read(harness: Harness, damage: str) -> None:
    setup(harness)
    harness.server.add("GET", PAGE, _page_with(damage))
    harness.server.add("PUT", PAGE, {"id": "123456", "title": "New", "version": {"number": 8}})
    result = run_input(
        harness,
        "confluence",
        "page",
        "update",
        "123456",
        doc={"expected_version": 7, "title": "New"},
    )
    assert result.code == 6
    assert result.error["code"] == "response_invalid"
    assert result.error["details"] == {"what": "page body"}
    assert harness.server.writes() == []


@pytest.mark.parametrize("damage", ["no_body", "null"])
def test_read_refuses_incomplete_body(harness: Harness, damage: str) -> None:
    setup(harness)
    harness.server.add("GET", PAGE, _page_with(damage))
    result = harness.run("confluence", "page", "read", "123456")
    assert result.error["code"] == "response_invalid"


def test_title_only_update_keeps_explicit_empty_body(harness: Harness) -> None:
    setup(harness)
    page = fixture("confluence_page.json")
    page["body"]["storage"]["value"] = ""
    harness.server.add("GET", PAGE, page)
    harness.server.add("PUT", PAGE, {"id": "123456", "title": "New", "version": {"number": 8}})
    result = run_input(
        harness,
        "confluence",
        "page",
        "update",
        "123456",
        doc={"expected_version": 7, "title": "New"},
    )
    assert result.code == 0
    (put,) = harness.server.writes()
    assert json.loads(put.content)["body"]["storage"]["value"] == ""


def test_body_update_does_not_need_current_body(harness: Harness) -> None:
    setup(harness)
    harness.server.add("GET", PAGE, _page_with("no_body"))
    harness.server.add("PUT", PAGE, {"id": "123456", "title": "Runbook", "version": {"number": 8}})
    result = run_input(
        harness,
        "confluence",
        "page",
        "update",
        "123456",
        doc={"expected_version": 7, "body_storage": "<p>new</p>"},
    )
    assert result.code == 0


def test_body_only_update_sends_exact_fetched_title(harness: Harness) -> None:
    setup(harness)
    page = fixture("confluence_page.json")
    page["title"] = " Runbook: on-call "
    harness.server.add("GET", PAGE, page)
    harness.server.add(
        "PUT", PAGE, {"id": "123456", "title": " Runbook: on-call ", "version": {"number": 8}}
    )
    result = run_input(
        harness,
        "confluence",
        "page",
        "update",
        "123456",
        doc={"expected_version": 7, "body_storage": "<p>new</p>"},
    )
    assert result.code == 0
    (put,) = harness.server.writes()
    assert json.loads(put.content)["title"] == " Runbook: on-call "


BAD_TITLES = [None, "", "   ", 42, ["Runbook"], {"value": "Runbook"}, "missing"]


def _page_with_title(title: Any) -> dict[str, Any]:
    page: dict[str, Any] = fixture("confluence_page.json")
    if title == "missing":
        del page["title"]
    else:
        page["title"] = title
    return page


@pytest.mark.parametrize("title", BAD_TITLES)
def test_body_only_update_refuses_invalid_fetched_title(harness: Harness, title: Any) -> None:
    setup(harness)
    harness.server.add("GET", PAGE, _page_with_title(title))
    harness.server.add("PUT", PAGE, UPDATED)
    result = run_input(
        harness,
        "confluence",
        "page",
        "update",
        "123456",
        doc={"expected_version": 7, "body_storage": "<p>new</p>"},
    )
    assert result.code == 6
    assert result.error["code"] == "response_invalid"
    assert result.error["details"] == {"what": "page title"}
    assert harness.server.writes() == []


@pytest.mark.parametrize("title", BAD_TITLES)
def test_read_refuses_invalid_title(harness: Harness, title: Any) -> None:
    setup(harness)
    harness.server.add("GET", PAGE, _page_with_title(title))
    result = harness.run("confluence", "page", "read", "123456")
    assert result.code == 6
    assert result.error["code"] == "response_invalid"
    assert result.error["details"] == {"what": "page title"}


BAD_VERSIONS = ["7", None, [7], {"number": "7"}, {"number": 7.0}, {"number": True}, {"number": 0}]


@pytest.mark.parametrize("version", BAD_VERSIONS)
def test_read_refuses_malformed_version(harness: Harness, version: Any) -> None:
    setup(harness)
    page = fixture("confluence_page.json")
    page["version"] = version
    harness.server.add("GET", PAGE, page)
    result = harness.run("confluence", "page", "read", "123456")
    assert result.code == 6
    assert result.error["code"] == "response_invalid"
    assert result.error["details"] == {"what": "page version"}


@pytest.mark.parametrize("version", BAD_VERSIONS)
def test_update_refuses_malformed_version_before_write(harness: Harness, version: Any) -> None:
    setup(harness)
    page = fixture("confluence_page.json")
    page["version"] = version
    harness.server.add("GET", PAGE, page)
    harness.server.add("PUT", PAGE, UPDATED)
    result = run_input(
        harness,
        "confluence",
        "page",
        "update",
        "123456",
        doc={"expected_version": 7, "title": "New"},
    )
    assert result.code == 6
    assert result.error["code"] == "response_invalid"
    assert result.error["details"] == {"what": "page version"}
    assert harness.server.writes() == []


def test_search_tolerates_malformed_version(harness: Harness) -> None:
    setup(harness)
    body = {"results": [{"id": "1", "type": "page", "title": "A", "version": "2"}]}
    harness.server.add("GET", SEARCH, body)
    result = harness.run("confluence", "page", "search", "--cql", "type = page")
    assert result.code == 0
    assert result.data["results"][0]["version"] is None


UPDATED = {"id": "123456", "title": "New", "version": {"number": 8}}


@pytest.mark.parametrize(
    "body",
    [
        None,
        b"not json",
        [],
        "ok",
        {},
        {k: v for k, v in UPDATED.items() if k != "id"},
        {**UPDATED, "id": "999"},
        {**UPDATED, "id": 123456},
        {k: v for k, v in UPDATED.items() if k != "title"},
        {**UPDATED, "title": "Old"},
        {k: v for k, v in UPDATED.items() if k != "version"},
        {**UPDATED, "version": None},
        {**UPDATED, "version": 8},
        {**UPDATED, "version": {}},
        {**UPDATED, "version": {"number": 7}},
        {**UPDATED, "version": {"number": 9}},
        {**UPDATED, "version": {"number": "8"}},
        {**UPDATED, "version": {"number": 8.0}},
        {**UPDATED, "version": {"number": True}},
    ],
)
def test_update_incomplete_success_is_outcome_unknown(harness: Harness, body: Any) -> None:
    setup(harness)
    harness.server.add("GET", PAGE, fixture("confluence_page.json"))
    if isinstance(body, bytes):
        harness.server.add("PUT", PAGE, handler=lambda _: httpx.Response(200, content=body))
    else:
        harness.server.add("PUT", PAGE, body)
    result = run_input(
        harness,
        "confluence",
        "page",
        "update",
        "123456",
        doc={"expected_version": 7, "title": "New"},
    )
    assert result.code == 6, result.out
    assert result.error["code"] == "outcome_unknown"
    assert "data" not in result.out
    if body != b"not json":
        assert "yojo confluence page read 123456 --full" in result.error["fix"]
        assert "version 8" in result.error["fix"]
    assert len(harness.server.writes()) == 1


def test_update_incomplete_success_marks_write_outcome_unknown() -> None:
    server = FakeServer()
    server.add("GET", PAGE, fixture("confluence_page.json"))
    server.add("PUT", PAGE, {**UPDATED, "version": {"number": 9}})
    client = ServiceClient(
        "confluence",
        CONFLUENCE,
        Credential(auth="pat", secret=CONFLUENCE_TOKEN),
        transport=server.transport(),
    )
    with pytest.raises(YojoError) as info:
        update(client, "123456", {"expected_version": 7, "title": "New"})
    assert info.value.code == "outcome_unknown"
    assert info.value.write_outcome == "unknown"


def test_search_next_start_counts_malformed_results(harness: Harness) -> None:
    setup(harness)
    body = {
        "results": [
            {"id": "1", "type": "page", "title": "A"},
            "junk",
            {"type": "page", "title": "no id"},
            {"id": "4", "type": "page", "title": "D"},
            {"id": "5", "type": "page", "title": "beyond limit"},
        ],
        "_links": {"next": "/rest/api/content/search?start=14"},
    }
    harness.server.add("GET", SEARCH, body)
    result = harness.run(
        "confluence", "page", "search", "--cql", "type = page", "--start", "10", "--limit", "4"
    )
    data = result.data
    assert [r["id"] for r in data["results"]] == ["1", "4"]
    assert data["size"] == 2
    assert data["next_start"] == 14
    assert harness.server.requests[0].url.params["start"] == "10"


def test_search_page_of_only_malformed_results_still_advances(harness: Harness) -> None:
    setup(harness)
    harness.server.add("GET", SEARCH, {"results": ["junk", {}], "_links": {"next": "x"}})
    result = harness.run("confluence", "page", "search", "--cql", "type = page", "--start", "5")
    assert result.data["results"] == []
    assert result.data["next_start"] == 7


def test_search_raw_empty_page_ends_despite_next_link(harness: Harness) -> None:
    setup(harness)
    body = {"results": [], "size": 0, "totalSize": 500, "_links": {"next": "x"}}
    harness.server.add("GET", SEARCH, body)
    result = harness.run("confluence", "page", "search", "--cql", "type = page", "--start", "5")
    assert result.code == 0
    assert result.data["results"] == []
    assert result.data["next_start"] is None


def test_create_accepts_canonical_space_key_case(harness: Harness) -> None:
    setup(harness)
    harness.server.add("POST", CONTENT, CREATED)
    doc = {"space_key": "eng", "title": "T", "body_storage": "<p>x</p>"}
    result = run_input(harness, "confluence", "page", "create", doc=doc)
    assert result.data == {
        "id": "9",
        "title": "T",
        "space_key": "ENG",
        "version": 1,
        "url": "https://confluence.example.test/confluence/pages/viewpage.action?pageId=9",
    }
    assert json.loads(harness.server.requests[0].content)["space"] == {"key": "eng"}


def test_create_case_insensitive_space_still_rejects_other_space(harness: Harness) -> None:
    setup(harness)
    harness.server.add("POST", CONTENT, {**CREATED, "space": {"key": "ENGX"}})
    doc = {"space_key": "eng", "title": "T", "body_storage": "<p>x</p>"}
    result = run_input(harness, "confluence", "page", "create", doc=doc)
    assert result.code == 6
    assert result.error["code"] == "outcome_unknown"
    assert "space" in result.error["details"]["reason"]


def test_create_accepts_server_trimmed_title_and_reports_it(harness: Harness) -> None:
    setup(harness)
    harness.server.add("POST", CONTENT, {**CREATED, "title": "Release notes"})
    doc = {"space_key": "ENG", "title": "  Release notes \t", "body_storage": "<p>x</p>"}
    result = run_input(harness, "confluence", "page", "create", doc=doc)
    assert result.data["title"] == "Release notes"
    assert json.loads(harness.server.requests[0].content)["title"] == "  Release notes \t"


@pytest.mark.parametrize("returned", ["release notes", "Release  notes", "Release notes!", "  "])
def test_create_rejects_title_changes_beyond_trimming(harness: Harness, returned: str) -> None:
    setup(harness)
    harness.server.add("POST", CONTENT, {**CREATED, "title": returned})
    doc = {"space_key": "ENG", "title": " Release notes ", "body_storage": "<p>x</p>"}
    result = run_input(harness, "confluence", "page", "create", doc=doc)
    assert result.code == 6
    assert result.error["code"] == "outcome_unknown"
    assert (
        result.error["details"]["reason"] == "create response did not confirm the submitted title"
    )


def test_create_rejects_page_id_with_trailing_newline(harness: Harness) -> None:
    setup(harness)
    harness.server.add("POST", CONTENT, {**CREATED, "id": "9\n"})
    doc = {"space_key": "ENG", "title": "T", "body_storage": "<p>x</p>"}
    result = run_input(harness, "confluence", "page", "create", doc=doc)
    assert result.code == 6
    assert result.error["details"]["reason"] == "create response did not include a valid page id"


@pytest.mark.parametrize(
    ("title", "exact"),
    [
        ("""Won't "fix" $HOME""", 'title = "Won\'t \\"fix\\" $HOME"'),
        (" Runbook\n", 'title = " Runbook" OR title = "Runbook"'),
    ],
    ids=["quoting", "trimmed"],
)
def test_create_outcome_unknown_fix_searches_every_accepted_title(
    harness: Harness, title: str, exact: str
) -> None:
    setup(harness)
    harness.server.add("POST", CONTENT, {**CREATED, "title": "Other"})
    doc = {"space_key": "ENG", "title": title, "body_storage": "<p>x</p>"}
    result = run_input(harness, "confluence", "page", "create", doc=doc)
    assert result.error["code"] == "outcome_unknown"
    fix = result.error["fix"]
    exact_cql = f'space = "ENG" AND type = page AND ({exact})'
    broad_cql = 'space = "ENG" AND type = page ORDER BY created DESC'
    search = ["yojo", "confluence", "page", "search", "--cql"]
    assert [shlex.split(c) for c in backticked(fix, "yojo confluence page search")] == [
        [*search, exact_cql],
        [*search, broad_cql],
    ]
    assert result.error["details"]["recovery_argv"] == {
        "exact_search": [*search, exact_cql],
        "broad_search": [*search, broad_cql],
    }
    assert "An empty exact-title search alone does not show the page was not created" in fix
    assert "yojo confluence page read ID --full" in fix
    assert "POSIX shells" in fix and "details.recovery_argv" in fix
    assert "does not retry writes" in fix
    assert len(harness.server.writes()) == 1


@pytest.mark.parametrize("title", SHELL_SENTINELS)
def test_create_outcome_unknown_commands_stay_literal_in_shells(
    harness: Harness, title: str, tmp_path: Path
) -> None:
    setup(harness)
    harness.server.add("POST", CONTENT, {**CREATED, "title": "Other"})
    doc = {"space_key": "ENG", "title": title, "body_storage": "<p>x</p>"}
    error = run_input(harness, "confluence", "page", "create", doc=doc).error
    argv = error["details"]["recovery_argv"]
    shell_dir = tmp_path / "shell"
    shell_dir.mkdir()
    commands = backticked(error["fix"], "yojo confluence page search")
    assert [run_in_posix_shell(c, shell_dir) for c in commands] == [
        argv["exact_search"],
        argv["broad_search"],
    ]
    assert [run_argv(a) for a in argv.values()] == list(argv.values())
    assert jql_quote(title) in argv["exact_search"][-1]


def test_update_accepts_server_trimmed_title_and_reports_it(harness: Harness) -> None:
    setup(harness)
    harness.server.add("GET", PAGE, fixture("confluence_page.json"))
    harness.server.add("PUT", PAGE, {**UPDATED, "title": "New"})
    result = run_input(
        harness,
        "confluence",
        "page",
        "update",
        "123456",
        doc={"expected_version": 7, "title": " New\n"},
    )
    assert result.data["title"] == "New"
    (put,) = harness.server.writes()
    assert json.loads(put.content)["title"] == " New\n"


def test_body_only_update_reports_trimmed_title(harness: Harness) -> None:
    setup(harness)
    page = fixture("confluence_page.json")
    page["title"] = " Runbook "
    harness.server.add("GET", PAGE, page)
    harness.server.add("PUT", PAGE, {"id": "123456", "title": "Runbook", "version": {"number": 8}})
    result = run_input(
        harness,
        "confluence",
        "page",
        "update",
        "123456",
        doc={"expected_version": 7, "body_storage": "<p>new</p>"},
    )
    assert result.data["title"] == "Runbook"
    (put,) = harness.server.writes()
    assert json.loads(put.content)["title"] == " Runbook "


def test_update_rejects_title_changes_beyond_trimming(harness: Harness) -> None:
    setup(harness)
    harness.server.add("GET", PAGE, fixture("confluence_page.json"))
    harness.server.add("PUT", PAGE, {**UPDATED, "title": "new"})
    result = run_input(
        harness,
        "confluence",
        "page",
        "update",
        "123456",
        doc={"expected_version": 7, "title": "New"},
    )
    assert result.code == 6
    assert (
        result.error["details"]["reason"] == "update response did not confirm the submitted title"
    )
