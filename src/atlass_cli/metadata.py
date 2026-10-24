"""Live Jira metadata: targeted paginated createmeta, issues, transitions, and editmeta."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from atlass_cli.contracts import AtlError
from atlass_cli.fields import FieldMeta
from atlass_cli.transport import ServiceClient, seg

META_PAGE_SIZE = 100
MAX_META_PAGES = 50


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


@dataclass(frozen=True)
class LiveTransition:
    id: str
    name: str
    to: str
    fields: dict[str, FieldMeta]


def _invalid(what: str) -> AtlError:
    return AtlError(
        "response_invalid",
        f"Jira returned an unexpected {what} response.",
        "Check that jira.base_url points at Jira Data Center 9+ including any context path.",
        {"what": what},
    )


def incomplete(what: str, details: dict[str, Any] | None = None) -> AtlError:
    return AtlError(
        "metadata_incomplete",
        f"Jira {what} metadata was truncated or inconsistent; atl will not guess.",
        "Retry; if it persists ask a Jira admin to check the Jira metadata endpoint (createmeta, "
        "transitions, or editmeta) and the fields on that screen.",
        {"what": what, **(details or {})},
    )


def paginate(client: ServiceClient, path: str, what: str) -> list[dict[str, Any]]:
    """Fetch every page of a Jira ``values`` paged response, failing closed if incomplete."""
    values: list[dict[str, Any]] = []
    start = 0
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


def find_issue_type(types: list[IssueType], name: str) -> IssueType | None:
    matches = [t for t in types if t.name == name]
    return matches[0] if len(matches) == 1 else None


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


def get_issue(client: ServiceClient, key: str) -> IssueSnapshot:
    raw = client.get(
        f"/rest/api/2/issue/{seg(key)}",
        {"fields": "status,attachment,project,issuetype,summary"},
    )
    if not isinstance(raw, dict) or not isinstance(raw.get("fields"), dict):
        raise _invalid("issue")
    try:
        fields = raw["fields"]
        attachments = fields.get("attachment") or []
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
        )
    except (KeyError, TypeError):
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
