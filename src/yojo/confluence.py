"""Confluence Data Center page operations via /rest/api/content."""

from __future__ import annotations

import re
import shlex
from typing import Any

from yojo.contracts import PAGE_ID_PATTERN, YojoError
from yojo.response import TextBudget
from yojo.transport import ServiceClient, jql_quote, sanitize_text, seg

PAGE_ID_RE = re.compile(PAGE_ID_PATTERN)

READ_EXPAND = "body.storage,version,space,ancestors"


def _invalid(what: str) -> YojoError:
    return YojoError(
        "response_invalid",
        f"Confluence returned an unexpected {what} response.",
        "Check that the Confluence URL (--base-url or confluence.base_url) points at Confluence "
        "Data Center including any context path.",
        {"what": what},
    )


def application_version(client: ServiceClient) -> str | None:
    """Confluence's version from the application-links manifest, or None when not reported.

    The manifest needs no login. A server that names another product is refused; one that answers
    without a usable manifest is reachable but unverified until login confirms it.
    """
    try:
        raw = client.get("/rest/applinks/1.0/manifest")
    except YojoError as exc:
        if exc.code in ("not_found", "response_invalid", "auth_failed", "forbidden"):
            return None
        raise
    type_id = raw.get("typeId") if isinstance(raw, dict) else None
    version = raw.get("version") if isinstance(raw, dict) else None
    if isinstance(type_id, str) and type_id.casefold() != "confluence":
        raise YojoError(
            "response_invalid",
            f"The server at {client.base_url} identified itself as "
            f"{sanitize_text(type_id, 40)}, not Confluence.",
            "Pass the Confluence application root including any context path, for example "
            "https://confluence.example.com/confluence.",
            {"what": "application manifest"},
        )
    if type_id is None or not isinstance(version, str) or not version.strip():
        return None
    return sanitize_text(version.strip(), 40)


def _version_number(page: dict[str, Any]) -> int | None:
    version = page.get("version")
    number = version.get("number") if isinstance(version, dict) else None
    return number if type(number) is int and number >= 1 else None


def _version(page: dict[str, Any]) -> int:
    number = _version_number(page)
    if number is None:
        raise _invalid("page version")
    return number


def _title(page: dict[str, Any]) -> str:
    title = page.get("title")
    if not isinstance(title, str) or not title.strip():
        raise _invalid("page title")
    return title


def _space_key(page: dict[str, Any]) -> str | None:
    space = page.get("space")
    key = space.get("key") if isinstance(space, dict) else None
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
    if page["id"] != page_id:
        raise YojoError(
            "response_invalid",
            f"Confluence returned content {sanitize_text(page['id'], 40)} when page {page_id} "
            "was requested.",
            f"Confirm {page_id} is the intended page ID with `yojo confluence page search`, and "
            "check that the Confluence URL is not routed through a rewriting proxy.",
            {"what": "page id", "requested_id": page_id},
        )
    return page


def page_url(client: ServiceClient, page_id: str) -> str:
    return client.url(f"/pages/viewpage.action?pageId={seg(page_id)}")


def read(client: ServiceClient, page_id: str, full: bool = False) -> dict[str, Any]:
    page = _get_page(client, page_id)
    ancestors = [
        str(a["id"]) for a in page.get("ancestors") or [] if isinstance(a, dict) and "id" in a
    ]
    body = _storage(page)
    shown, truncated = TextBudget(full).fit(body)
    return {
        "id": page["id"],
        "title": _title(page),
        "type": str(page.get("type", "")),
        "space_key": _space_key(page),
        "version": _version(page),
        "url": page_url(client, page_id),
        "ancestors": ancestors[-5:],
        "body_storage": shown,
        "body_chars": len(body),
        "truncated": truncated,
        "more": shlex.join(["yojo", "confluence", "page", "read", page_id, "--full"])
        if truncated
        else None,
        "content_trust": "untrusted",
    }


def search(client: ServiceClient, cql: str, start: int, limit: int) -> dict[str, Any]:
    raw = client.get(
        "/rest/api/content/search",
        {"cql": cql, "start": start, "limit": limit, "expand": "space,version"},
    )
    if not isinstance(raw, dict) or not isinstance(raw.get("results"), list):
        raise _invalid("search")
    consumed = raw["results"][:limit]
    results = []
    for item in consumed:
        if not isinstance(item, dict) or "id" not in item:
            continue
        results.append(
            {
                "id": str(item["id"]),
                "title": str(item.get("title", "")),
                "type": str(item.get("type", "")),
                "space_key": _space_key(item),
                "version": _version_number(item),
            }
        )
    has_next = isinstance(raw.get("_links"), dict) and "next" in raw["_links"]
    return {
        "start": start,
        "limit": limit,
        "size": len(results),
        "next_start": start + len(consumed) if has_next and consumed else None,
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
    try:
        client.announce(f"Creating page {sanitize_text(doc['title'], 80)!r} in {doc['space_key']}")
        created = client.request("POST", "/rest/api/content", json_body=body)
    except YojoError as exc:
        if exc.code == "outcome_unknown":
            recovery = _create_unknown(
                client,
                str(exc.details.get("reason", "create outcome unknown")),
                doc["space_key"],
                doc["title"],
            )
            exc.fix = recovery.fix
            exc.details["recovery_argv"] = recovery.details["recovery_argv"]
        raise
    reason = _create_mismatch(created, doc["title"], doc["space_key"])
    if reason is not None:
        raise _create_unknown(client, reason, doc["space_key"], doc["title"])
    assert isinstance(created, dict)
    return {
        "id": created["id"],
        "title": created["title"],
        "space_key": created["space"]["key"],
        "version": 1,
        "url": page_url(client, created["id"]),
    }


def _create_unknown(client: ServiceClient, reason: str, space_key: str, title: str) -> YojoError:
    """Build recovery guidance covering every title the create confirmation would accept."""
    titles = list(dict.fromkeys(t for t in (title, title.strip()) if t))
    exact = (
        f"space = {jql_quote(space_key)} AND type = page AND ("
        + " OR ".join(f"title = {jql_quote(t)}" for t in titles)
        + ")"
    )
    broad = f"space = {jql_quote(space_key)} AND type = page ORDER BY created DESC"
    argv = {
        "exact_search": ["yojo", "confluence", "page", "search", "--cql", exact],
        "broad_search": ["yojo", "confluence", "page", "search", "--cql", broad],
    }
    error = client.unknown_outcome(
        reason,
        f"Do not retry yet. Search for the page under its submitted and edge-trimmed titles: "
        f"`{shlex.join(argv['exact_search'])}`. If that finds nothing, list the space's newest "
        f"pages: `{shlex.join(argv['broad_search'])}`. Read every plausible match with "
        "`yojo confluence page read ID --full` to confirm whether it is yours. An empty "
        "exact-title search alone does not show the page was not created, and results can "
        "shift between pages, so a scan with no match does not show it either. If no page "
        "matches or you cannot tell, ask a human to confirm the page does not exist before any "
        "retry. yojo does not retry writes. The commands above are quoted for POSIX shells (sh, "
        "bash, zsh) only; in cmd.exe, PowerShell, or a program, pass each list in "
        "details.recovery_argv as separate "
        "arguments.",
    )
    error.details["recovery_argv"] = argv
    return error


def _title_confirmed(submitted: str, returned: Any) -> bool:
    """Accept the submitted title, or the same nonblank title with edge whitespace trimmed."""
    return (
        isinstance(returned, str)
        and bool(returned.strip())
        and returned.strip() == submitted.strip()
    )


def _create_mismatch(created: Any, title: str, space_key: str) -> str | None:
    """Return why a create response does not confirm the submitted page, or None if it does."""
    if not isinstance(created, dict):
        return "create response was not a JSON object"
    page_id = created.get("id")
    if not isinstance(page_id, str) or not PAGE_ID_RE.fullmatch(page_id):
        return "create response did not include a valid page id"
    if created.get("type") != "page":
        return "create response did not confirm the content type page"
    if not _title_confirmed(title, created.get("title")):
        return "create response did not confirm the submitted title"
    returned_space = _space_key(created)
    if returned_space is None or returned_space.casefold() != space_key.casefold():
        return "create response did not confirm the submitted space"
    raw_version = created.get("version")
    number = raw_version.get("number") if isinstance(raw_version, dict) else None
    if type(number) is not int or number != 1:
        return "create response did not confirm version 1"
    return None


def _stale(page_id: str, expected: int, current: int) -> YojoError:
    return YojoError(
        "version_conflict",
        f"Page {page_id} is at version {current}, not the expected version {expected}.",
        f"Re-read the page with `yojo confluence page read {page_id} --full`, merge your changes "
        "into the "
        f"current content, and resubmit with expected_version {current}.",
        {"id": page_id, "expected_version": expected, "current_version": current},
    )


def update(client: ServiceClient, page_id: str, doc: dict[str, Any]) -> dict[str, Any]:
    expected = doc["expected_version"]
    current = _get_page(client, page_id)
    if current.get("type") != "page":
        raise YojoError(
            "input_invalid",
            f"Content {page_id} is not a page (type {sanitize_text(current.get('type'), 40)}); "
            "nothing was written.",
            "Pass the ID of a Confluence page; `yojo confluence page update` does not edit blog "
            "posts or other content types.",
            {"id": page_id, "type": sanitize_text(current.get("type"), 40)},
        )
    current_version = _version(current)
    if current_version != expected:
        raise _stale(page_id, expected, current_version)
    title = doc["title"] if "title" in doc else _title(current)
    storage = doc["body_storage"] if "body_storage" in doc else _storage(current)
    body = {
        "id": page_id,
        "type": "page",
        "title": title,
        "version": {"number": expected + 1},
        "body": {"storage": {"value": storage, "representation": "storage"}},
    }
    try:
        client.announce(f"Updating page {page_id} to version {expected + 1}")
        updated = client.request("PUT", f"/rest/api/content/{seg(page_id)}", json_body=body)
    except YojoError as exc:
        if exc.code == "conflict":
            raise YojoError(
                "version_conflict",
                f"Page {page_id} changed while updating (409 Conflict); nothing was overwritten.",
                f"Re-read the page with `yojo confluence page read {page_id} --full`, merge, and "
                "resubmit "
                "with the new expected_version.",
                {"id": page_id, "expected_version": expected, **exc.details},
                write_outcome="rejected",
            ) from None
        raise
    reason = _update_mismatch(updated, page_id, title, expected + 1)
    if reason is not None:
        raise client.unknown_outcome(
            reason,
            f"Run `yojo confluence page read {page_id} --full` and compare its title, version, and "
            f"body with what you submitted (version {expected + 1} expected) before retrying. yojo "
            "does not retry writes.",
        )
    assert isinstance(updated, dict)
    return {
        "id": page_id,
        "title": updated["title"],
        "version": expected + 1,
        "previous_version": expected,
        "url": page_url(client, page_id),
    }


def _update_mismatch(updated: Any, page_id: str, title: str, version: int) -> str | None:
    """Return why an update response does not confirm the submitted page, or None if it does."""
    if not isinstance(updated, dict):
        return "update response was not a JSON object"
    if updated.get("id") != page_id:
        return "update response did not confirm the page id"
    if not _title_confirmed(title, updated.get("title")):
        return "update response did not confirm the submitted title"
    raw_version = updated.get("version")
    number = raw_version.get("number") if isinstance(raw_version, dict) else None
    if type(number) is not int or number != version:
        return "update response did not confirm the next page version"
    return None
