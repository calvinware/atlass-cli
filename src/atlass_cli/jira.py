"""Jira commands: create, attachments, comments, search, and config drift checks."""

from __future__ import annotations

import os
import re
import secrets
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

from atlass_cli.config import RESERVED_FIELDS, Config, JiraConfig, ProjectConfig, require_jira
from atlass_cli.contracts import ISSUE_KEY_PATTERN, AtlError
from atlass_cli.features import jql_quote
from atlass_cli.fields import FieldMeta, FieldProblem, resolve, serialize
from atlass_cli.metadata import (
    create_fields,
    create_issue_types,
    find_issue_type,
    get_editmeta,
    get_issue,
    get_transitions,
)
from atlass_cli.transport import ServiceClient, sanitize_text, seg
from atlass_cli.workflow import (
    Check,
    TransitionContext,
    checks_pass,
    evaluate,
    field_collision,
    requires_editmeta,
)

ISSUE_KEY_RE = re.compile(ISSUE_KEY_PATTERN)
SYSTEM_CREATE_FIELDS = frozenset({"project", "issuetype", "summary"})
UPLOAD_CHUNK = 64 * 1024
SEARCH_FIELDS = "summary,status,issuetype,project,assignee,updated"
ATTESTATION_NOTE = (
    "The human_confirmation attestation is caller-supplied text recorded for accountability. "
    "atl does not verify the identity of the person named."
)


def browse_url(client: ServiceClient, key: str) -> str:
    return client.url(f"/browse/{seg(key)}")


# --------------------------------------------------------------------------- create


def _unmet(what: str, checks: list[Check]) -> AtlError:
    failed = [c for c in checks if c.status not in ("pass", "not_applicable")]
    return AtlError(
        "requirements_unmet",
        f"{what} preflight failed ({len(failed)} check(s)); nothing was sent to Jira.",
        "Resolve each failed check using its fix, then retry.",
        {"checks": [c.as_dict() for c in checks]},
    )


def _create_preflight(
    client: ServiceClient,
    project: ProjectConfig,
    issue_type_name: str,
    values: dict[str, Any],
    description: str | None,
    checks: list[Check],
    required: tuple[str, ...],
    type_key: str,
) -> tuple[str | None, dict[str, Any]]:
    """Validate supplied values against live create metadata. Returns (issue type id, fields).

    ``required`` names the friendly fields that ``create.<type_key>.required`` demands; each
    must have a non-blank value and be on this issue type's create screen.
    """
    missing = [friendly for friendly in required if _blank(values.get(friendly))]
    issue_type = find_issue_type(create_issue_types(client, project.key), issue_type_name)
    if issue_type is None:
        checks.append(
            Check(
                "issue_type",
                "fail",
                f"Issue type {issue_type_name!r} is not creatable in {project.key}.",
                f"Confirm {issue_type_name!r} exists in {project.key} and your account can create "
                "it; run `atl config check` to see drift.",
            )
        )
        checks.extend(_required_missing(f, issue_type_name, type_key) for f in missing)
        return None, {}
    checks.append(
        Check("issue_type", "pass", f"{issue_type_name!r} is creatable in {project.key}.")
    )
    live = create_fields(client, project.key, issue_type.id)

    for friendly in missing:
        checks.append(_required_check(project, friendly, issue_type_name, type_key, live))

    payload: dict[str, Any] = {}
    owners: dict[str, str] = {}
    for friendly, value in values.items():
        ref = project.fields[friendly]
        check_id = f"field:{friendly}"
        try:
            meta = resolve(ref, live)
            if meta is None:
                checks.append(
                    Check(
                        check_id,
                        "fail",
                        f"{ref.label!r} is not on the {issue_type_name} create screen.",
                        f"Ask a Jira admin to add {ref.label!r} to the {issue_type_name} create "
                        f"screen, or fix fields.{friendly} in atl.yaml.",
                    )
                )
                continue
            if meta.id in owners:
                checks.append(field_collision(check_id, owners[meta.id], friendly, meta))
                continue
            payload[meta.id] = serialize(ref, meta, value, require_set=False)
            owners[meta.id] = friendly
            checks.append(Check(check_id, "pass", f"{meta.name!r} ({meta.id}) is valid."))
        except FieldProblem as problem:
            checks.append(Check(check_id, "fail", problem.message, problem.fix, problem.details))

    if description is not None:
        if "description" in owners:
            collision = field_collision(
                "field:description", owners["description"], "description", live["description"]
            )
            checks.append(collision)
        elif "description" in live:
            owners["description"] = "description"
        else:
            checks.append(
                Check(
                    "field:description",
                    "fail",
                    f"Description is not on the {issue_type_name} create screen.",
                    "Remove description from the input.",
                )
            )

    for field_id, meta in sorted(live.items()):
        if (
            meta.required
            and not meta.has_default
            and field_id not in SYSTEM_CREATE_FIELDS
            and field_id not in owners
        ):
            checks.append(_missing_live_field(meta, issue_type_name))
    return issue_type.id, payload


def _required_missing(friendly: str, issue_type_name: str, type_key: str) -> Check:
    return Check(
        f"required:{friendly}",
        "fail",
        f"atl.yaml create.{type_key}.required requires {friendly!r} when creating a "
        f"{issue_type_name}.",
        f"Supply {friendly!r} in the input fields object.",
    )


def _required_check(
    project: ProjectConfig,
    friendly: str,
    issue_type_name: str,
    type_key: str,
    live: dict[str, FieldMeta],
) -> Check:
    """Explain a missing required value, including when it cannot be supplied on this screen."""
    ref = project.fields[friendly]
    check_id = f"required:{friendly}"
    try:
        meta = resolve(ref, live)
    except FieldProblem as problem:
        return Check(check_id, "fail", problem.message, problem.fix, problem.details)
    if meta is None:
        return Check(
            check_id,
            "fail",
            f"atl.yaml create.{type_key}.required requires {friendly!r}, but {ref.label!r} is "
            f"not on the {issue_type_name} create screen.",
            f"Ask a Jira admin to add {ref.label!r} to the {issue_type_name} create screen, or fix "
            f"fields.{friendly} or create.{type_key}.required in atl.yaml.",
        )
    return _required_missing(friendly, issue_type_name, type_key)


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or value == []


def _missing_live_field(meta: FieldMeta, issue_type_name: str) -> Check:
    return Check(
        f"create_field:{meta.id}",
        "fail",
        f"Jira requires {meta.name!r} ({meta.id}) when creating a {issue_type_name}.",
        f"Map {meta.name!r} under fields in atl.yaml and supply it in the input `fields` object.",
        {"field_id": meta.id},
    )


def _input_fields(
    project: ProjectConfig, raw: dict[str, Any], *, allow_reserved: bool
) -> dict[str, Any]:
    unknown = sorted(name for name in raw if name not in project.fields)
    if unknown:
        raise AtlError(
            "input_invalid",
            f"Unknown field name(s) for {project.key}: {', '.join(unknown)}.",
            f"Use friendly names defined in atl.yaml: {', '.join(sorted(project.fields))}.",
            {"unknown": unknown},
        )
    reserved = sorted(name for name in raw if name in RESERVED_FIELDS)
    if reserved and not allow_reserved:
        raise AtlError(
            "input_invalid",
            f"Supply {', '.join(reserved)} as top-level properties, not inside fields.",
            "Move acceptance_criteria and feature_key to the top level of the input.",
            {"reserved": reserved},
        )
    return dict(raw)


def _create(
    client: ServiceClient,
    project: ProjectConfig,
    type_id: str,
    summary: str,
    description: str | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "project": {"key": project.key},
        "issuetype": {"id": type_id},
        "summary": summary,
        **payload,
    }
    if description is not None:
        fields["description"] = description
    created = client.request("POST", "/rest/api/2/issue", json_body={"fields": fields})
    if not isinstance(created, dict) or not isinstance(created.get("key"), str):
        raise client.unknown_outcome("create response did not include an issue key")
    return created


def story_create(
    client: ServiceClient, jira: JiraConfig, project: ProjectConfig, doc: dict[str, Any]
) -> dict[str, Any]:
    checks: list[Check] = []
    extra = _input_fields(project, doc.get("fields", {}), allow_reserved=False)

    ac = doc.get("acceptance_criteria")
    values: dict[str, Any] = {}
    if isinstance(ac, str) and ac.strip():
        values["acceptance_criteria"] = ac
        checks.append(Check("acceptance_criteria", "pass", "Acceptance criteria supplied."))
    else:
        checks.append(
            Check(
                "acceptance_criteria",
                "fail",
                "acceptance_criteria is missing or blank.",
                "Write the acceptance criteria for this story and pass them as "
                "acceptance_criteria. atl never generates acceptance criteria.",
            )
        )

    feature_key = doc.get("feature_key")
    if isinstance(feature_key, str) and ISSUE_KEY_RE.match(feature_key):
        checks.append(_check_feature(client, jira, project, feature_key))
        values["feature_link"] = feature_key
    else:
        checks.append(
            Check(
                "feature_key",
                "fail",
                "feature_key is missing or not an issue key.",
                "Choose the parent feature yourself (candidates: `atl jira feature find --text "
                "'<story summary>'`) and pass its key as feature_key. atl never chooses a feature.",
            )
        )

    values.update(extra)
    # acceptance_criteria and feature_link are always required and checked above.
    required = tuple(f for f in project.story_required if f not in RESERVED_FIELDS)

    description = doc.get("description")
    type_id, payload = _create_preflight(
        client, project, project.story_type, values, description, checks, required, "story"
    )
    if not checks_pass(checks) or type_id is None:
        raise _unmet("Story create", checks)
    created = _create(client, project, type_id, doc["summary"], description, payload)
    return {
        "key": created["key"],
        "id": str(created.get("id", "")),
        "url": browse_url(client, created["key"]),
        "project": project.key,
        "issue_type": project.story_type,
        "checks": [c.as_dict() for c in checks],
    }


def _check_feature(
    client: ServiceClient, jira: JiraConfig, project: ProjectConfig, key: str
) -> Check:
    try:
        feature = get_issue(client, key)
    except AtlError as exc:
        if exc.code != "not_found":
            raise
        return Check(
            "feature_key",
            "fail",
            f"Feature {key} was not found or is not visible.",
            "Check the key, or find candidates with `atl jira feature find`.",
        )
    expected = jira.projects.get(feature.project, project).feature_type
    if feature.issue_type != expected:
        return Check(
            "feature_key",
            "fail",
            f"{key} is a {feature.issue_type}, not a {expected}.",
            f"Pass the key of a {expected} issue as feature_key.",
            {"issue_type": feature.issue_type},
        )
    return Check(
        "feature_key", "pass", f"{key} is a {expected}: {sanitize_text(feature.summary, 120)}"
    )


def feature_create(
    client: ServiceClient, project: ProjectConfig, doc: dict[str, Any]
) -> dict[str, Any]:
    confirmation = doc.get("human_confirmation")
    if project.feature_create_policy == "human_confirmed" and confirmation is None:
        raise approval_required(project)
    checks: list[Check] = []
    values = _input_fields(project, doc.get("fields", {}), allow_reserved=True)
    description = doc.get("description")
    type_id, payload = _create_preflight(
        client,
        project,
        project.feature_type,
        values,
        description,
        checks,
        project.feature_required,
        "feature",
    )
    if not checks_pass(checks) or type_id is None:
        raise _unmet("Feature create", checks)
    created = _create(client, project, type_id, doc["summary"], description, payload)
    attestation = None
    if confirmation is not None:
        attestation = {
            "confirmed_by": confirmation["confirmed_by"],
            "statement": confirmation["statement"],
            "identity_verified": False,
            "note": ATTESTATION_NOTE,
        }
    return {
        "key": created["key"],
        "id": str(created.get("id", "")),
        "url": browse_url(client, created["key"]),
        "project": project.key,
        "issue_type": project.feature_type,
        "policy": project.feature_create_policy,
        "attestation": attestation,
        "checks": [c.as_dict() for c in checks],
    }


def approval_required(project: ProjectConfig) -> AtlError:
    return AtlError(
        "approval_required",
        f"Project {project.key} requires human confirmation before creating a feature; nothing "
        "was sent to Jira.",
        "Ask a human to approve creating this feature, then add human_confirmation "
        '{"confirmed_by": "<name>", "statement": "<what they approved>"} to the input. '
        + ATTESTATION_NOTE,
        {"policy": project.feature_create_policy, "identity_verified": False},
    )


# --------------------------------------------------------------------------- attachments


class _FileChanged(Exception):
    pass


def _header_filename(name: str) -> str:
    return "".join("_" if ch in '"\\' or not ch.isprintable() else ch for ch in name) or "file"


def _multipart(
    handle: BinaryIO, size: int, filename: str, boundary: str
) -> tuple[Iterator[bytes], int]:
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{_header_filename(filename)}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()

    def body() -> Iterator[bytes]:
        yield head
        remaining = size
        while remaining > 0:
            chunk = handle.read(min(UPLOAD_CHUNK, remaining))
            if not chunk:
                raise _FileChanged("shrank")
            remaining -= len(chunk)
            if remaining == 0 and handle.read(1):
                raise _FileChanged("grew")
            yield chunk
        if size == 0 and handle.read(1):
            raise _FileChanged("grew")
        yield tail

    return body(), len(head) + size + len(tail)


def attachment_add(client: ServiceClient, jira: JiraConfig, key: str, path: Path) -> dict[str, Any]:
    limit = jira.max_attachment_bytes
    try:
        info = path.stat()
    except OSError:
        raise AtlError(
            "file_invalid",
            f"File not found or unreadable: {path}",
            "Pass --file with the path to an existing regular file.",
            {"path": str(path)},
        ) from None
    if not stat.S_ISREG(info.st_mode):
        raise AtlError(
            "file_invalid",
            f"Not a regular file: {path}",
            "Pass --file with the path to a regular file.",
            {"path": str(path)},
        )
    if info.st_size > limit:
        raise AtlError(
            "attachment_too_large",
            f"{path.name} is {info.st_size} bytes; jira.max_attachment_bytes is {limit}.",
            "Attach a smaller file (compress or split it), or raise jira.max_attachment_bytes in "
            "atl.yaml if the team agrees.",
            {"size": info.st_size, "limit": limit},
        )
    boundary = secrets.token_hex(16)
    try:
        handle = path.open("rb")
    except OSError:
        raise AtlError(
            "file_invalid",
            f"Cannot open {path}.",
            "Check file permissions.",
            {"path": str(path)},
        ) from None
    name = os.path.basename(path)
    with handle:
        body, length = _multipart(handle, info.st_size, name, boundary)
        try:
            result = client.request(
                "POST",
                f"/rest/api/2/issue/{seg(key)}/attachments",
                content=body,
                headers={
                    "X-Atlassian-Token": "no-check",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(length),
                },
            )
        except _FileChanged as change:
            raise AtlError(
                "file_invalid",
                f"{path.name} {change} while uploading; the upload was aborted before completion "
                f"and never exceeded {info.st_size} bytes.",
                f"Wait until the file is fully written, confirm no partial attachment exists on "
                f"{key}, then retry.",
                {"path": str(path), "declared_size": info.st_size, "dispatched": "aborted"},
            ) from None
    attachments = _confirmed_attachments(client, key, result, name, info.st_size)
    return {"key": key, "attachments": attachments}


def _confirmed_attachments(
    client: ServiceClient, key: str, result: Any, name: str, size: int
) -> list[dict[str, Any]]:
    """Return the attachments Jira confirmed; anything incomplete means the outcome is unknown."""

    def unknown(reason: str) -> AtlError:
        return client.unknown_outcome(
            reason,
            f"Open {key} in the Jira issue view and check whether an attachment named {name!r} "
            f"of {size} bytes was added before retrying `atl jira attachment add`. atl does not "
            "retry writes.",
        )

    if not isinstance(result, list) or not result:
        raise unknown("attachment response was not a non-empty list")
    attachments: list[dict[str, Any]] = []
    for index, item in enumerate(result):
        if not isinstance(item, dict):
            raise unknown(f"attachment response item {index} was not an object")
        item_id, filename, item_size = item.get("id"), item.get("filename"), item.get("size")
        if not isinstance(item_id, str) or not item_id.strip():
            raise unknown(f"attachment response item {index} has no id")
        if not isinstance(filename, str) or not filename.strip():
            raise unknown(f"attachment response item {index} has no filename")
        if type(item_size) is not int or item_size < 0:
            raise unknown(f"attachment response item {index} has no valid size")
        attachments.append({"id": item_id, "filename": filename, "size": item_size})
    sent = {name, _header_filename(name)}
    if not any(a["filename"] in sent and a["size"] == size for a in attachments):
        raise unknown("attachment response did not confirm the uploaded file name and size")
    return attachments


# --------------------------------------------------------------------------- comment / search


def comment_add(client: ServiceClient, key: str, body: str) -> dict[str, Any]:
    if not body.strip():
        raise AtlError(
            "input_invalid",
            "The comment body is empty.",
            "Provide non-empty comment text in --body-file or on stdin.",
        )
    result = client.request(
        "POST", f"/rest/api/2/issue/{seg(key)}/comment", json_body={"body": body}
    )
    if not isinstance(result, dict) or "id" not in result:
        raise client.unknown_outcome("comment response did not include an id")
    return {"key": key, "comment_id": str(result["id"])}


def _name(value: Any, key: str = "name") -> str | None:
    if isinstance(value, dict) and isinstance(value.get(key), str):
        return str(value[key])
    return None


def search(client: ServiceClient, jql: str, start: int, limit: int) -> dict[str, Any]:
    raw = client.get(
        "/rest/api/2/search",
        {"jql": jql, "startAt": start, "maxResults": limit, "fields": SEARCH_FIELDS},
    )
    if not isinstance(raw, dict) or not isinstance(raw.get("issues"), list):
        raise AtlError(
            "response_invalid",
            "Jira returned an unexpected search response.",
            "Check that jira.base_url points at Jira Data Center.",
        )
    issues = []
    for item in raw["issues"][:limit]:
        if not isinstance(item, dict):
            continue
        fields = item.get("fields") or {}
        issues.append(
            {
                "key": str(item.get("key", "")),
                "id": str(item.get("id", "")),
                "summary": fields.get("summary")
                if isinstance(fields.get("summary"), str)
                else None,
                "status": _name(fields.get("status")),
                "issue_type": _name(fields.get("issuetype")),
                "project": _name(fields.get("project"), "key"),
                "assignee": _name(fields.get("assignee"), "displayName"),
                "updated": fields.get("updated")
                if isinstance(fields.get("updated"), str)
                else None,
            }
        )
    raw_total = raw.get("total")
    total = raw_total if isinstance(raw_total, int) else start + len(issues)
    next_start = start + len(issues) if issues and start + len(issues) < total else None
    return {
        "jql": jql,
        "start": start,
        "limit": limit,
        "total": total,
        "next_start": next_start,
        "issues": issues,
    }


# --------------------------------------------------------------------------- config check


def _in_source(project: ProjectConfig, status: str, issue_project: Any, issue_status: Any) -> bool:
    return (
        issue_project == project.key
        and isinstance(issue_status, str)
        and issue_status.casefold() == status.casefold()
    )


def _sample_issue(
    client: ServiceClient, project: ProjectConfig, status: str, explicit: str | None
) -> str | None:
    """Return an issue that Jira reports in ``project`` and ``status``, or None."""
    if explicit is not None:
        snapshot = get_issue(client, explicit)
        if snapshot.key == explicit and _in_source(
            project, status, snapshot.project, snapshot.status
        ):
            return explicit
    raw = client.get(
        "/rest/api/2/search",
        {
            "jql": f"project = {jql_quote(project.key)} AND status = {jql_quote(status)} "
            "ORDER BY updated DESC",
            "startAt": 0,
            "maxResults": 1,
            "fields": "status,project",
        },
    )
    issues = raw.get("issues") if isinstance(raw, dict) else None
    if not isinstance(issues, list) or not issues or not isinstance(issues[0], dict):
        return None
    hit = issues[0]
    raw_fields = hit.get("fields")
    fields: dict[str, Any] = raw_fields if isinstance(raw_fields, dict) else {}
    if isinstance(hit.get("key"), str) and _in_source(
        project, status, _name(fields.get("project"), "key"), _name(fields.get("status"))
    ):
        return str(hit["key"])
    return None


def _drift(check: Check) -> Check:
    if check.status == "fail":
        check.status = "drift"
    return check


def config_check(client: ServiceClient, config: Config, explicit_issue: str | None) -> list[Check]:
    jira = require_jira(config)
    checks: list[Check] = []
    for project in jira.projects.values():
        checks.extend(_check_project_create(client, project))
        checks.extend(_check_project_transitions(client, project, explicit_issue))
    return checks


def _check_project_create(client: ServiceClient, project: ProjectConfig) -> list[Check]:
    checks: list[Check] = []
    prefix = project.key
    try:
        types = create_issue_types(client, project.key)
    except AtlError as exc:
        if exc.code not in ("metadata_incomplete", "not_found", "forbidden"):
            raise
        return [
            Check(
                f"{prefix}:create_metadata",
                "unverified" if exc.code == "metadata_incomplete" else "drift",
                exc.message,
                exc.fix,
            )
        ]
    names = {t.name for t in types}
    for name in project.allowed_issue_types:
        if name in names:
            checks.append(Check(f"{prefix}:issue_type:{name}", "pass", f"{name!r} is creatable."))
        else:
            checks.append(
                Check(
                    f"{prefix}:issue_type:{name}",
                    "drift",
                    f"{name!r} is in allowed_issue_types but not creatable in {project.key}.",
                    f"Remove {name!r} from allowed_issue_types or ask a Jira admin to add it to "
                    f"{project.key}'s issue type scheme.",
                    {"live": sorted(names)},
                )
            )

    targets = [
        (
            project.story_type,
            "story",
            list(RESERVED_FIELDS) + [f for f in project.story_required if f not in RESERVED_FIELDS],
        ),
        (project.feature_type, "feature", list(project.feature_required)),
    ]
    for type_name, type_key, friendly_names in targets:
        issue_type = find_issue_type(types, type_name)
        if issue_type is None:
            ids = sorted(t.id for t in types if t.name == type_name)
            if len(ids) > 1:
                checks.append(
                    Check(
                        f"{prefix}:{type_name}:create_fields",
                        "drift",
                        f"{len(ids)} creatable issue types in {project.key} are named "
                        f"{type_name!r}; atl cannot tell which create screen to check.",
                        f"Ask a Jira admin to rename the duplicate {type_name!r} issue types in "
                        f"{project.key}'s issue type scheme.",
                        {"issue_type_ids": ids},
                    )
                )
            continue
        try:
            live = create_fields(client, project.key, issue_type.id)
        except AtlError as exc:
            if exc.code != "metadata_incomplete":
                raise
            checks.append(
                Check(f"{prefix}:{type_name}:create_fields", "unverified", exc.message, exc.fix)
            )
            continue
        resolved: set[str] = set()
        for friendly in friendly_names:
            ref = project.fields[friendly]
            check_id = f"{prefix}:{type_name}:field:{friendly}"
            try:
                meta = resolve(ref, live)
            except FieldProblem as problem:
                checks.append(
                    Check(check_id, "drift", problem.message, problem.fix, problem.details)
                )
                continue
            if meta is None:
                checks.append(
                    Check(
                        check_id,
                        "drift",
                        f"{ref.label!r} is not on the {type_name} create screen.",
                        _off_screen_fix(friendly, type_key),
                    )
                )
                continue
            resolved.add(meta.id)
            problem_check = _encoding_check(check_id, ref.encoding, meta)
            checks.append(problem_check)
        for field_id, meta in sorted(live.items()):
            if (
                meta.required
                and not meta.has_default
                and field_id not in SYSTEM_CREATE_FIELDS
                and field_id not in resolved
                and field_id != "description"
            ):
                check = _missing_live_field(meta, type_name)
                check.id = f"{prefix}:{type_name}:{check.id}"
                checks.append(_drift(check))
    return checks


def _off_screen_fix(friendly: str, type_key: str) -> str:
    if type_key == "story" and friendly in RESERVED_FIELDS:
        return f"Fix fields.{friendly} in atl.yaml or add the field to the create screen."
    return (
        f"Fix fields.{friendly} or create.{type_key}.required in atl.yaml, or add the field to "
        "the create screen."
    )


_ENCODING_SCHEMAS: dict[str, tuple[set[str], str | None]] = {
    "text": ({"string"}, None),
    "issue_key_string": ({"string", "any"}, None),
    "issue_key_object": ({"issuelink", "issue"}, None),
    "single_option": ({"option"}, None),
    "multi_option": ({"array"}, "option"),
    "resolution_option": ({"resolution"}, None),
}


def _encoding_check(check_id: str, encoding: str, meta: FieldMeta) -> Check:
    types, items = _ENCODING_SCHEMAS[encoding]
    schema_type = meta.schema.get("type")
    ok = schema_type in types and (items is None or meta.schema.get("items") == items)
    if (
        encoding in ("single_option", "multi_option", "resolution_option")
        and meta.allowed_values is None
    ):
        ok = False
    if ok:
        return Check(check_id, "pass", f"{meta.name!r} ({meta.id}) matches encoding {encoding!r}.")
    return Check(
        check_id,
        "drift",
        f"{meta.name!r} ({meta.id}) has schema {meta.schema or '{}'}, incompatible with encoding "
        f"{encoding!r} (or allowed values are not exposed).",
        "Change the field's encoding or mapping in atl.yaml.",
        {"field_id": meta.id, "schema": meta.schema},
    )


def _check_project_transitions(
    client: ServiceClient, project: ProjectConfig, explicit_issue: str | None
) -> list[Check]:
    checks: list[Check] = []
    for config in project.transitions.values():
        for source in config.from_statuses:
            prefix = f"{project.key}:transition:{config.name}:{source}"
            sample = _sample_issue(client, project, source, explicit_issue)
            if sample is None:
                checks.append(
                    Check(
                        prefix,
                        "unverified",
                        f"No accessible {project.key} issue is in {source!r}, so transition "
                        f"{config.name!r} could not be probed.",
                        f"Re-run with `atl config check --issue KEY` using a {project.key} issue "
                        f"currently in {source!r}.",
                    )
                )
                continue
            issue = get_issue(client, sample)
            if issue.key != sample or not _in_source(project, source, issue.project, issue.status):
                checks.append(
                    Check(
                        prefix,
                        "unverified",
                        f"Sample issue {sample} is no longer a {project.key} issue in {source!r}, "
                        f"so transition {config.name!r} could not be probed.",
                        f"Re-run `atl config check`, or pass --issue KEY for a {project.key} "
                        f"issue currently in {source!r}.",
                        {"sample_issue": sample, "status": issue.status},
                    )
                )
                continue
            try:
                transitions = get_transitions(client, sample)
                editmeta = (
                    get_editmeta(client, sample)
                    if requires_editmeta(project, config, transitions)
                    else None
                )
            except AtlError as exc:
                if exc.code != "metadata_incomplete":
                    raise
                checks.append(
                    Check(
                        prefix,
                        "unverified",
                        exc.message,
                        exc.fix,
                        {**exc.details, "sample_issue": sample},
                    )
                )
                continue
            result = evaluate(
                project,
                config.to,
                TransitionContext(issue=issue, transitions=transitions, editmeta=editmeta),
                check_attachments=False,
            )
            for check in result.checks:
                if check.status == "not_applicable":
                    continue
                check.id = f"{prefix}:{check.id}"
                check.details = {**check.details, "sample_issue": sample}
                checks.append(_drift(check))
    return checks
