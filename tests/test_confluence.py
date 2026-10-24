from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from atlass_cli.auth import Credential
from atlass_cli.confluence import update
from atlass_cli.contracts import AtlError
from atlass_cli.transport import ServiceClient
from tests.conftest import (
    CONFLUENCE,
    CONFLUENCE_TOKEN,
    FakeServer,
    Harness,
    confluence_path,
    fixture,
)

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
    assert result.data == {"id": "9", "title": "T", "space_key": "ENG", "version": 1}
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
    if body != b"not json":
        assert (
            """atl confluence page search --cql 'space = "ENG" AND title = "T"'"""
            in (result.error["fix"])
        )
    assert len(harness.server.writes()) == 1


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
    assert result.data == {"id": "123456", "title": "New", "version": 8, "previous_version": 7}
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
    assert "atl confluence page read 123456" in result.error["fix"]
    assert len(harness.server.writes()) == 1


def test_update_requires_title_or_body(harness: Harness) -> None:
    setup(harness)
    result = run_input(
        harness, "confluence", "page", "update", "123456", doc={"expected_version": 7}
    )
    assert result.error["code"] == "input_invalid"
    assert harness.server.requests == []


def test_confluence_not_configured(harness: Harness) -> None:
    harness.write_config(
        "version: 1\njira:\n  base_url: https://jira.example.test\n  projects: []\n"
    )
    result = harness.run("confluence", "page", "read", "1")
    assert result.error["code"] == "config_invalid"


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
        assert "atl confluence page read 123456" in result.error["fix"]
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
    with pytest.raises(AtlError) as info:
        update(client, "123456", {"expected_version": 7, "title": "New"})
    assert info.value.code == "outcome_unknown"
    assert info.value.write_outcome == "unknown"
