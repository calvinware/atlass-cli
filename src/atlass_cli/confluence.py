"""Confluence Data Center page operations via /rest/api/content."""

from __future__ import annotations

from typing import Any

from atlass_cli.contracts import AtlError
from atlass_cli.transport import ServiceClient, seg

READ_EXPAND = "body.storage,version,space,ancestors"


def _invalid(what: str) -> AtlError:
    return AtlError(
        "response_invalid",
        f"Confluence returned an unexpected {what} response.",
        "Check that confluence.base_url points at Confluence Data Center including any context "
        "path.",
        {"what": what},
    )


def _version(page: dict[str, Any]) -> int:
    number = (page.get("version") or {}).get("number")
    if type(number) is not int:
        raise _invalid("page version")
    return number


def _space_key(page: dict[str, Any]) -> str | None:
    key = (page.get("space") or {}).get("key")
    return key if isinstance(key, str) else None


def _storage(page: dict[str, Any]) -> str:
    """Return the storage body; an empty body is accepted only when Confluence sends one."""
    body = page.get("body")
    storage = body.get("storage") if isinstance(body, dict) else None
    value = storage.get("value") if isinstance(storage, dict) else None
    if not isinstance(value, str):
        raise _invalid("page body")
    return value


def _get_page(client: ServiceClient, page_id: str) -> dict[str, Any]:
    page = client.get(f"/rest/api/content/{seg(page_id)}", {"expand": READ_EXPAND})
    if not isinstance(page, dict) or not isinstance(page.get("id"), str):
        raise _invalid("page")
    return page


def read(client: ServiceClient, page_id: str) -> dict[str, Any]:
    page = _get_page(client, page_id)
    ancestors = [
        str(a["id"]) for a in page.get("ancestors") or [] if isinstance(a, dict) and "id" in a
    ]
    return {
        "id": page["id"],
        "title": str(page.get("title", "")),
        "type": str(page.get("type", "")),
        "space_key": _space_key(page),
        "version": _version(page),
        "ancestors": ancestors,
        "body_storage": _storage(page),
        "content_trust": "untrusted",
    }


def search(client: ServiceClient, cql: str, start: int, limit: int) -> dict[str, Any]:
    raw = client.get(
        "/rest/api/content/search",
        {"cql": cql, "start": start, "limit": limit, "expand": "space,version"},
    )
    if not isinstance(raw, dict) or not isinstance(raw.get("results"), list):
        raise _invalid("search")
    results = []
    for item in raw["results"][:limit]:
        if not isinstance(item, dict) or "id" not in item:
            continue
        number = (item.get("version") or {}).get("number")
        results.append(
            {
                "id": str(item["id"]),
                "title": str(item.get("title", "")),
                "type": str(item.get("type", "")),
                "space_key": _space_key(item),
                "version": number if type(number) is int else None,
            }
        )
    has_next = isinstance(raw.get("_links"), dict) and "next" in raw["_links"]
    return {
        "cql": cql,
        "start": start,
        "limit": limit,
        "size": len(results),
        "next_start": start + len(results) if has_next and results else None,
        "results": results,
        "content_trust": "untrusted",
    }


def create(client: ServiceClient, doc: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": "page",
        "title": doc["title"],
        "space": {"key": doc["space_key"]},
        "body": {"storage": {"value": doc["body_storage"], "representation": "storage"}},
    }
    if "parent_id" in doc:
        body["ancestors"] = [{"id": doc["parent_id"]}]
    created = client.request("POST", "/rest/api/content", json_body=body)
    if not isinstance(created, dict) or not isinstance(created.get("id"), str):
        raise client.unknown_outcome("create response did not include a page id")
    number = (created.get("version") or {}).get("number")
    return {
        "id": created["id"],
        "title": str(created.get("title", doc["title"])),
        "space_key": _space_key(created) or doc["space_key"],
        "version": number if type(number) is int else 1,
    }


def _stale(page_id: str, expected: int, current: int) -> AtlError:
    return AtlError(
        "version_conflict",
        f"Page {page_id} is at version {current}, not the expected version {expected}.",
        f"Re-read the page with `atl confluence page read {page_id}`, merge your changes into the "
        f"current content, and resubmit with expected_version {current}.",
        {"id": page_id, "expected_version": expected, "current_version": current},
    )


def update(client: ServiceClient, page_id: str, doc: dict[str, Any]) -> dict[str, Any]:
    expected = doc["expected_version"]
    current = _get_page(client, page_id)
    current_version = _version(current)
    if current_version != expected:
        raise _stale(page_id, expected, current_version)
    title = doc.get("title", str(current.get("title", "")))
    storage = doc["body_storage"] if "body_storage" in doc else _storage(current)
    body = {
        "id": page_id,
        "type": str(current.get("type") or "page"),
        "title": title,
        "version": {"number": expected + 1},
        "body": {"storage": {"value": storage, "representation": "storage"}},
    }
    try:
        updated = client.request("PUT", f"/rest/api/content/{seg(page_id)}", json_body=body)
    except AtlError as exc:
        if exc.code == "conflict":
            raise AtlError(
                "version_conflict",
                f"Page {page_id} changed while updating (409 Conflict); nothing was overwritten.",
                f"Re-read the page with `atl confluence page read {page_id}`, merge, and resubmit "
                "with the new expected_version.",
                {"id": page_id, "expected_version": expected, **exc.details},
                write_outcome="rejected",
            ) from None
        raise
    reason = _update_mismatch(updated, page_id, title, expected + 1)
    if reason is not None:
        raise client.unknown_outcome(
            reason,
            f"Run `atl confluence page read {page_id}` and compare its title, version, and body "
            f"with what you submitted (version {expected + 1} expected) before retrying. atl "
            "does not retry writes.",
        )
    return {
        "id": page_id,
        "title": title,
        "version": expected + 1,
        "previous_version": expected,
    }


def _update_mismatch(updated: Any, page_id: str, title: str, version: int) -> str | None:
    """Return why an update response does not confirm the submitted page, or None if it does."""
    if not isinstance(updated, dict):
        return "update response was not a JSON object"
    if updated.get("id") != page_id:
        return "update response did not confirm the page id"
    if updated.get("title") != title:
        return "update response did not confirm the submitted title"
    raw_version = updated.get("version")
    number = raw_version.get("number") if isinstance(raw_version, dict) else None
    if type(number) is not int or number != version:
        return "update response did not confirm the next page version"
    return None
