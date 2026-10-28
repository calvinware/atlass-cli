"""Live Jira metadata: createmeta, issues, transitions, editmeta, fields, links, permissions."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from yojo.contracts import PROJECT_KEY_PATTERN, YojoError
from yojo.fields import FieldMeta
from yojo.transport import ServiceClient, sanitize_text, seg

META_PAGE_SIZE = 100
MAX_META_PAGES = 50
MAX_PROJECTS = 5000
MAX_STATUSES = 500
PROJECT_KEY_RE = re.compile(PROJECT_KEY_PATTERN)


@dataclass(frozen=True)
class IssueType:
    id: str
    name: str


@dataclass(frozen=True)
class IssueSnapshot:
    key: str
    project: str
    status: str
    issue_type: str
    summary: str
    attachments: tuple[str, ...]
    assignee: str | None = None
    updated: str | None = None
    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LinkType:
    id: str
    name: str
    inward: str
    outward: str


@dataclass(frozen=True)
class LiveTransition:
    id: str
    name: str
    to: str
    fields: dict[str, FieldMeta]


def _invalid(what: str) -> YojoError:
    return YojoError(
        "response_invalid",
        f"Jira returned an unexpected {what} response.",
        "Check that the Jira URL (--base-url or jira.base_url) points at Jira Data Center 9+ "
        "including any context path.",
        {"what": what},
    )


def incomplete(what: str, details: dict[str, Any] | None = None) -> YojoError:
    return YojoError(
        "metadata_incomplete",
        f"Jira {what} metadata was truncated or inconsistent; yojo will not guess.",
        "Retry; if it persists ask a Jira admin to check the Jira metadata endpoint (createmeta, "
        "transitions, or editmeta) and the fields on that screen.",
        {"what": what, **(details or {})},
    )


def paginate(client: ServiceClient, path: str, what: str) -> list[dict[str, Any]]:
    """Fetch every page of a Jira ``values`` paged response, failing closed if incomplete."""
    values: list[dict[str, Any]] = []
    start = 0
    known_total: int | None = None
    for _ in range(MAX_META_PAGES):
        page = client.get(path, {"startAt": start, "maxResults": META_PAGE_SIZE})
        if not isinstance(page, dict) or not isinstance(page.get("values"), list):
            raise _invalid(what)
        raw_batch = page["values"]
        if not all(isinstance(v, dict) for v in raw_batch):
            raise incomplete(what, {"start_at": start, "reason": "malformed entry"})
        batch = raw_batch
        reported_start = page.get("startAt")
        if reported_start is not None and (
            type(reported_start) is not int or reported_start != start
        ):
            raise incomplete(what, {"start_at": start, "reported_start": reported_start})
        values.extend(batch)
        is_last = page.get("isLast")
        total = page.get("total")
        if total is not None and (type(total) is not int or total < 0):
            raise incomplete(what, {"start_at": start, "reason": "invalid total"})
        if total is not None and known_total is not None and total != known_total:
            raise incomplete(
                what, {"start_at": start, "total": total, "previous_total": known_total}
            )
        # A total from any earlier page still bounds pages that omit it.
        known_total = total = known_total if total is None else total
        if total is not None and len(values) > total:
            raise incomplete(what, {"received": len(values), "total": total})
        if is_last is True:
            if total is not None and len(values) != total:
                raise incomplete(what, {"received": len(values), "total": total})
            return values
        if is_last is None and total is not None and len(values) == total:
            return values
        if not batch:
            # Without isLast or total, an empty page is the only end marker Jira sends.
            if is_last is None and total is None:
                return values
            raise incomplete(what, {"received": len(values), "total": total})
        start += len(batch)
    raise incomplete(what, {"received": len(values), "max_pages": MAX_META_PAGES})


def create_issue_types(client: ServiceClient, project: str) -> list[IssueType]:
    raw = paginate(client, f"/rest/api/2/issue/createmeta/{seg(project)}/issuetypes", "issue type")
    types: list[IssueType] = []
    for item in raw:
        if not isinstance(item.get("id"), str) or not isinstance(item.get("name"), str):
            raise incomplete("issue type", {"reason": "missing id or name"})
        types.append(IssueType(id=item["id"], name=item["name"]))
    return types


def create_fields(client: ServiceClient, project: str, issue_type_id: str) -> dict[str, FieldMeta]:
    raw = paginate(
        client,
        f"/rest/api/2/issue/createmeta/{seg(project)}/issuetypes/{seg(issue_type_id)}",
        "create field",
    )
    fields: dict[str, FieldMeta] = {}
    for item in raw:
        field_id = item.get("fieldId")
        if not isinstance(field_id, str) or not field_id or field_id in fields:
            raise incomplete("create field", {"reason": "missing or duplicate fieldId"})
        fields[field_id] = _parse_field("create field", field_id, item)
    return fields


def _parse_field(what: str, field_id: Any, raw: Any) -> FieldMeta:
    """Parse one field entry, refusing entries whose flags cannot be trusted."""
    if (
        not isinstance(field_id, str)
        or not field_id
        or not isinstance(raw, dict)
        or not isinstance(raw.get("required"), bool)
        or ("hasDefaultValue" in raw and not isinstance(raw["hasDefaultValue"], bool))
    ):
        raise incomplete(
            what,
            {"field_id": str(field_id)[:100], "reason": "malformed field entry or flags"},
        )
    return FieldMeta.parse(field_id, raw)


def _parse_field_map(what: str, raw: Any) -> dict[str, FieldMeta]:
    if not isinstance(raw, dict):
        raise incomplete(what, {"reason": "fields missing or not an object"})
    return {fid: _parse_field(what, fid, meta) for fid, meta in raw.items()}


def _basename(filename: str) -> str:
    return PureWindowsPath(PurePosixPath(filename).name).name


ISSUE_BASE_FIELDS = (
    "status",
    "attachment",
    "project",
    "issuetype",
    "summary",
    "assignee",
    "updated",
)


def get_issue(client: ServiceClient, key: str, extra: Iterable[str] = ()) -> IssueSnapshot:
    wanted = list(dict.fromkeys([*ISSUE_BASE_FIELDS, *extra]))
    raw = client.get(f"/rest/api/2/issue/{seg(key)}", {"fields": ",".join(wanted)})
    if not isinstance(raw, dict) or not isinstance(raw.get("fields"), dict):
        raise _invalid("issue")
    try:
        fields = raw["fields"]
        attachments = fields.get("attachment") or []
        assignee = fields.get("assignee")
        return IssueSnapshot(
            key=str(raw["key"]),
            project=str(fields["project"]["key"]),
            status=str(fields["status"]["name"]),
            issue_type=str(fields["issuetype"]["name"]),
            summary=str(fields.get("summary") or ""),
            attachments=tuple(
                _basename(str(a["filename"]))
                for a in attachments
                if isinstance(a, dict) and isinstance(a.get("filename"), str)
            ),
            assignee=str(assignee.get("displayName") or assignee.get("name"))
            if isinstance(assignee, dict)
            else None,
            updated=fields["updated"] if isinstance(fields.get("updated"), str) else None,
            values={f: fields.get(f) for f in extra},
        )
    except (KeyError, TypeError, AttributeError):
        raise _invalid("issue") from None


def get_transitions(client: ServiceClient, key: str) -> list[LiveTransition]:
    raw = client.get(f"/rest/api/2/issue/{seg(key)}/transitions", {"expand": "transitions.fields"})
    if not isinstance(raw, dict) or not isinstance(raw.get("transitions"), list):
        raise _invalid("transitions")
    transitions: list[LiveTransition] = []
    for item in raw["transitions"]:
        try:
            transitions.append(
                LiveTransition(
                    id=str(item["id"]),
                    name=str(item["name"]),
                    to=str(item["to"]["name"]),
                    # expand=transitions.fields was requested, so an absent, null, or malformed
                    # map means the screen is unknown; only an explicit {} means "no fields".
                    fields=_parse_field_map("transition field", item.get("fields")),
                )
            )
        except (KeyError, TypeError, AttributeError):
            raise _invalid("transitions") from None
    return transitions


def get_editmeta(client: ServiceClient, key: str) -> dict[str, FieldMeta]:
    raw = client.get(f"/rest/api/2/issue/{seg(key)}/editmeta")
    if not isinstance(raw, dict) or not isinstance(raw.get("fields"), dict):
        raise _invalid("editmeta")
    return _parse_field_map("edit field", raw["fields"])


def all_fields(client: ServiceClient) -> dict[str, FieldMeta]:
    """Every field Jira knows, by ID, for naming fields outside a create or edit screen."""
    raw = client.get("/rest/api/2/field")
    if not isinstance(raw, list):
        raise _invalid("field list")
    fields: dict[str, FieldMeta] = {}
    for item in raw:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not item["id"].strip()
            or not isinstance(item.get("name"), str)
            or not item["name"].strip()
        ):
            raise incomplete("field list", {"reason": "malformed entry"})
        if item["id"] in fields:
            raise incomplete("field list", {"reason": "duplicate field ID"})
        fields[item["id"]] = FieldMeta.parse(item["id"], {**item, "required": False})
    return fields


def link_types(client: ServiceClient) -> list[LinkType]:
    raw = client.get("/rest/api/2/issueLinkType")
    items = raw.get("issueLinkTypes") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise _invalid("issue link type")
    types: list[LinkType] = []
    for item in items:
        values = [item.get(k) if isinstance(item, dict) else None for k in LINK_KEYS]
        if not all(isinstance(v, str) and v for v in values):
            raise incomplete("issue link type", {"reason": "missing id, name, inward or outward"})
        types.append(LinkType(*(str(v) for v in values)))
    return types


LINK_KEYS = ("id", "name", "inward", "outward")


def my_permissions(client: ServiceClient, key: str, names: Iterable[str]) -> dict[str, bool | None]:
    """Whether the logged-in user holds each permission on ``key``; None when Jira did not say."""
    raw = client.get("/rest/api/2/mypermissions", {"issueKey": key})
    perms = raw.get("permissions") if isinstance(raw, dict) else None
    result: dict[str, bool | None] = {}
    for name in names:
        entry = perms.get(name) if isinstance(perms, dict) else None
        have = entry.get("havePermission") if isinstance(entry, dict) else None
        result[name] = have if isinstance(have, bool) else None
    return result


def attachment_settings(client: ServiceClient) -> tuple[bool | None, int | None]:
    """Return (attachments enabled, server upload limit in bytes); None when not reported."""
    raw = client.get("/rest/api/2/attachment/meta")
    if not isinstance(raw, dict):
        return None, None
    enabled = raw.get("enabled")
    limit = raw.get("uploadLimit")
    return (
        enabled if isinstance(enabled, bool) else None,
        limit if type(limit) is int and limit > 0 else None,
    )


def server_version(client: ServiceClient) -> str:
    """Jira's own version from ``serverInfo``, which answers without a login."""
    raw = client.get("/rest/api/2/serverInfo")
    version = raw.get("version") if isinstance(raw, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise _invalid("server info")
    return sanitize_text(version.strip(), 40)


def projects(client: ServiceClient) -> list[dict[str, str]]:
    """Every project the user can see, as key and name; refused rather than shown partly."""
    raw = client.get("/rest/api/2/project")
    if not isinstance(raw, list):
        raise _invalid("project list")
    if len(raw) > MAX_PROJECTS:
        raise incomplete("project list", {"received": len(raw), "max": MAX_PROJECTS})
    found: dict[str, dict[str, str]] = {}
    for item in raw:
        key = item.get("key") if isinstance(item, dict) else None
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(key, str) or not PROJECT_KEY_RE.fullmatch(key) or key in found:
            raise incomplete("project list", {"reason": "missing, invalid, or duplicate key"})
        if not isinstance(name, str) or not name.strip():
            raise incomplete("project list", {"reason": "missing name", "project": key})
        found[key] = {"key": key, "name": sanitize_text(name, 255)}
    return sorted(found.values(), key=lambda p: p["key"])


def project_statuses(client: ServiceClient, project: str) -> list[str]:
    """Status names used by any issue type in ``project``, in Jira's order, without repeats."""
    raw = client.get(f"/rest/api/2/project/{seg(project)}/statuses")
    if not isinstance(raw, list):
        raise _invalid("project status")
    names: dict[str, str] = {}
    for issue_type in raw:
        statuses = issue_type.get("statuses") if isinstance(issue_type, dict) else None
        if not isinstance(statuses, list):
            raise incomplete("project status", {"reason": "missing statuses", "project": project})
        for status in statuses:
            name = status.get("name") if isinstance(status, dict) else None
            if not isinstance(name, str) or not name.strip():
                raise incomplete("project status", {"reason": "missing name", "project": project})
            names.setdefault(name.casefold(), name)
    if len(names) > MAX_STATUSES:
        raise incomplete("project status", {"received": len(names), "max": MAX_STATUSES})
    return list(names.values())
