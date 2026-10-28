"""Generic Jira operations: issues, links, comments, attachments, search, and config checks.

Every write checks everything it can before sending and refuses with all problems at once; a
success is returned only when Jira confirms the result.
"""

from __future__ import annotations

import os
import re
import secrets
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, cast

from yojo.config import Config, FieldRef, JiraConfig, ProjectConfig
from yojo.contracts import ISSUE_KEY_PATTERN, YojoError
from yojo.fields import SYSTEM_FIELDS, FieldMeta, confirmed, resolve, simplify
from yojo.metadata import (
    IssueType,
    LinkType,
    all_fields,
    attachment_settings,
    create_fields,
    create_issue_types,
    get_editmeta,
    get_issue,
    link_types,
    my_permissions,
)
from yojo.resolve import Candidate, Unresolved, UserLookup, pick
from yojo.response import TextBudget, nested_str
from yojo.retry import PROGRAM, Edit, placeholder, posix
from yojo.transport import ServiceClient, sanitize_text, seg
from yojo.workflow import (
    Assignment,
    Check,
    PlannedField,
    attempted,
    failed,
    missing_required,
    plan_field,
    refusal,
)

ISSUE_KEY_RE = re.compile(ISSUE_KEY_PATTERN)
UPLOAD_CHUNK = 64 * 1024
SEARCH_FIELDS = "summary,status,issuetype,assignee"
MAX_LISTED = 10


def browse_url(client: ServiceClient, key: str) -> str:
    return client.url(f"/browse/{seg(key)}")


def _issue_cmd(*args: str) -> str:
    return posix([PROGRAM, "jira", *args])


def _status_of(client: ServiceClient, key: str) -> str | None:
    """Best-effort status read after a confirmed write; the write stands either way."""
    try:
        return get_issue(client, key).status
    except YojoError:
        return None


def _permission_check(client: ServiceClient, key: str, permission: str, action: str) -> Check:
    check_id = f"permission:{permission}"
    try:
        have = my_permissions(client, key, [permission])[permission]
    except YojoError as exc:
        if exc.code not in ("not_found", "response_invalid", "forbidden"):
            raise
        have = None
    if have is True:
        return Check(check_id, "pass", f"You may {action} on {key}.")
    if have is False:
        return Check(
            check_id,
            "fail",
            f"Your Jira account may not {action} on {key}.",
            f"Ask a Jira admin for the {permission} permission in {key.rpartition('-')[0]}.",
        )
    return Check(
        check_id,
        "unverified",
        f"Jira did not report whether you may {action} on {key}.",
        "Run the command again; if it repeats, ask a Jira admin to check that "
        "/rest/api/2/mypermissions is reachable.",
    )


def _issue_exists(
    client: ServiceClient, key: str, check_id: str = "issue", extra: tuple[str, ...] = ()
) -> tuple[Any, Check]:
    try:
        issue = get_issue(client, key, extra)
    except YojoError as exc:
        if exc.code != "not_found":
            raise
        return None, Check(
            check_id,
            "fail",
            f"{key} was not found or you cannot see it.",
            f"Check the key; {_issue_cmd('search', '--jql', 'key = ' + key)} shows whether it "
            "is visible to you.",
        )
    return issue, Check(check_id, "pass", f"{issue.key} exists ({issue.status}).")


# --------------------------------------------------------------------------- create


@dataclass
class IssueRequest:
    project: str | None = None
    type: str | None = None
    summary: str | None = None
    description: str | None = None
    assignments: list[Assignment] = field(default_factory=list)


def _pick_type(types: list[IssueType], given: str, project: str) -> tuple[IssueType | None, Check]:
    pool = [Candidate(label=t.name, value=t, token=t.name) for t in types]
    try:
        chosen: IssueType = pick(given, pool, "issue type", f"in {project}").value
    except Unresolved as exc:
        choices = "|".join(t.name for t in types[:MAX_LISTED])
        edit = Edit("flag", "--type", exc.replacement or placeholder(choices))
        return None, Check("issue_type", "fail", exc.message, exc.fix, exc.details(), edit)
    return chosen, Check("issue_type", "pass", f"{chosen.name} can be created in {project}.")


def issue_create(
    client: ServiceClient, jira: JiraConfig, request: IssueRequest, users: UserLookup
) -> dict[str, Any]:
    assert request.project is not None
    project = request.project
    checks: list[Check] = []
    if not request.summary or not request.summary.strip():
        checks.append(
            Check(
                "summary",
                "fail",
                "The summary is missing.",
                "Add --summary 'one-line title'.",
                retry=Edit("flag", "--summary", placeholder("summary")),
            )
        )
    types = create_issue_types(client, project)
    issue_type: IssueType | None = None
    if not types:
        checks.append(
            Check(
                "issue_type",
                "fail",
                f"You cannot create issues in {project}, or the project does not exist.",
                f"Check the project key and ask a Jira admin for create permission in {project}.",
            )
        )
    elif request.type is None:
        names = [t.name for t in types[:MAX_LISTED]]
        checks.append(
            Check(
                "issue_type",
                "fail",
                "The issue type is missing.",
                f"Add --type with one of: {', '.join(repr(n) for n in names)}.",
                {"allowed": names},
                Edit("flag", "--type", placeholder("|".join(names))),
            )
        )
    else:
        issue_type, check = _pick_type(types, request.type, project)
        checks.append(check)

    payload: dict[str, Any] = {}
    changed: list[PlannedField] = []
    if issue_type is None:
        checks.extend(
            Check(
                f"field:{a.name}",
                "unverified",
                f"{a.name!r} cannot be checked until the issue type is known.",
                "Resolve the issue_type check first.",
            )
            for a in request.assignments
        )
        raise refusal(f"Creating an issue in {project}", checks)

    live = create_fields(client, project, issue_type.id)
    aliases = jira.aliases(project)
    scope = f"on the {issue_type.name} create screen in {project}"
    covered: dict[str, PlannedField] = {}
    for assignment in request.assignments:
        planned, check = plan_field(
            assignment, live, aliases, users, covered, scope=scope, reserved=SYSTEM_FIELDS
        )
        checks.append(check)
        if planned is not None:
            payload[planned.id] = planned.payload
            changed.append(planned)
    if request.description is not None:
        if "description" in covered:
            checks.append(
                Check(
                    "field:description",
                    "fail",
                    "Description is given twice (--description and --set).",
                    "Give the description once.",
                    retry=Edit("drop", covered["description"].given),
                )
            )
        elif "description" not in live:
            checks.append(
                Check(
                    "field:description",
                    "fail",
                    f"Description is not on the {issue_type.name} create screen in {project}.",
                    "Remove --description.",
                )
            )
        else:
            payload["description"] = request.description
            changed.append(PlannedField("description", "description", "Description", None))
            covered["description"] = changed[-1]

    what = f"to create a {issue_type.name} in {project}"
    reported = attempted(checks)
    for field_id, meta in sorted(live.items()):
        required = meta.required and not meta.has_default and field_id not in SYSTEM_FIELDS
        if required and field_id not in covered and field_id not in reported:
            checks.append(_required_create(meta, what))
            reported.add(field_id)
    configured = jira.project(project)
    for name in configured.required_for(issue_type.name) if configured else ():
        checks.append(_configured_required(name, live, aliases, covered, reported, issue_type))

    if failed(checks):
        raise refusal(f"Creating a {issue_type.name} in {project}", checks)

    summary = request.summary or ""
    client.announce(
        f"Creating a {issue_type.name} in {project}: {sanitize_text(summary, 60)!r}"
        + (f", setting {', '.join(f.name for f in changed)}" if changed else "")
    )
    created = _create(client, project, issue_type.id, summary, payload)
    key = created["key"]
    return {
        "key": key,
        "url": browse_url(client, key),
        "status": _status_of(client, key),
        "issue_type": issue_type.name,
        "project": project,
        "changed": [{"field": "Summary", "id": "summary"}, *(f.as_dict() for f in changed)],
    }


def _required_create(meta: FieldMeta, what: str) -> Check:
    if meta.id == "description":
        return Check(
            "required:description",
            "fail",
            f"Jira requires Description {what}.",
            "Add --description 'text'.",
            {"field_id": "description"},
            Edit("flag", "--description", placeholder("description")),
        )
    return missing_required(meta, what)


def _configured_required(
    name: str,
    live: dict[str, FieldMeta],
    aliases: dict[str, FieldRef],
    covered: dict[str, PlannedField],
    reported: set[str],
    issue_type: IssueType,
) -> Check:
    check_id = f"rule:{name}"
    try:
        meta = resolve(name, live, aliases)
    except Unresolved as exc:
        return Check(
            check_id,
            "fail",
            f"yojo.yaml requires {name!r} for a {issue_type.name}, but it is not on the create "
            "screen.",
            f"Ask a Jira admin to add {name!r} to the {issue_type.name} create screen, or fix "
            f"create.{issue_type.name}.required in yojo.yaml. {exc.fix}",
            exc.details(),
        )
    if meta.id in covered:
        return Check(check_id, "pass", f"{meta.name} is supplied as yojo.yaml requires.")
    if meta.id in reported:
        return Check(check_id, "not_applicable", f"{meta.name} is already reported as required.")
    if meta.id == "description":
        return _required_create(meta, f"(yojo.yaml) for a {issue_type.name}")
    check = missing_required(meta, f"(yojo.yaml) for a {issue_type.name}")
    check.id = check_id
    check.message = f"yojo.yaml requires {meta.shown} when creating a {issue_type.name}."
    return check


def _create(
    client: ServiceClient, project: str, type_id: str, summary: str, payload: dict[str, Any]
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        **payload,
        "project": {"key": project},
        "issuetype": {"id": type_id},
        "summary": summary,
    }
    search = _issue_cmd(
        "search", "--jql", f"project = {project} AND created >= -1h ORDER BY created DESC"
    )
    fix = (
        f"Do not retry yet. List recent issues with `{search}` (widen -1h if needed) and look "
        f"for the summary {sanitize_text(summary, 120)!r}; open each candidate to confirm it is "
        "yours. Only if none is, run the create again. yojo does not retry writes."
    )
    try:
        created = client.request("POST", "/rest/api/2/issue", json_body={"fields": fields})
    except YojoError as exc:
        if exc.code == "outcome_unknown":
            exc.fix = fix
        raise
    reason = _create_mismatch(created, project)
    if reason is not None:
        raise client.unknown_outcome(reason, fix)
    return {"id": created["id"], "key": created["key"]}


def _create_mismatch(created: Any, project_key: str) -> str | None:
    """Return why a create response does not confirm a new issue in the project, or None."""
    if not isinstance(created, dict):
        return "create response was not a JSON object"
    key = created.get("key")
    if not isinstance(key, str) or not ISSUE_KEY_RE.fullmatch(key):
        return "create response did not include a valid issue key"
    if not key.startswith(f"{project_key}-"):
        return f"create response key is not in project {project_key}"
    issue_id = created.get("id")
    if not isinstance(issue_id, str) or not issue_id.strip():
        return "create response did not include an issue id"
    return None


# --------------------------------------------------------------------------- get


def resolve_names(
    names: list[str], pool: dict[str, FieldMeta], aliases: dict[str, FieldRef]
) -> list[FieldMeta]:
    """Resolve every name or raise one input error that lists every problem."""
    found: list[FieldMeta] = []
    problems: list[dict[str, Any]] = []
    for name in names:
        try:
            meta = resolve(name, pool, aliases)
        except Unresolved as exc:
            problems.append({"message": exc.message, "fix": exc.fix, **exc.details()})
            continue
        if meta.id not in {m.id for m in found}:
            found.append(meta)
    if problems:
        raise YojoError(
            "input_invalid",
            f"{len(problems)} field name(s) did not match a Jira field; nothing was read.",
            " ".join(p["fix"] for p in problems),
            {"problems": problems},
        )
    return found


def issue_get(
    client: ServiceClient, jira: JiraConfig, key: str, names: list[str], full: bool
) -> dict[str, Any]:
    selected = (
        resolve_names(names, all_fields(client), jira.aliases(key.rpartition("-")[0]))
        if names
        else []
    )
    issue = get_issue(client, key, [m.id for m in selected])
    values: list[dict[str, Any]] = []
    trimmed: list[str] = []
    budget = TextBudget(full)
    for meta in selected:
        value, cut = budget.fit(simplify(issue.values.get(meta.id)))
        if cut:
            trimmed.append(meta.name)
        values.append({"field": meta.name, "id": meta.id, "value": value, "truncated": cut})
    more = (
        _issue_cmd("issue", "get", issue.key, "--fields", ",".join(trimmed), "--full")
        if trimmed
        else None
    )
    return {
        "key": issue.key,
        "url": browse_url(client, issue.key),
        "summary": issue.summary or None,
        "status": issue.status,
        "issue_type": issue.issue_type,
        "project": issue.project,
        "assignee": issue.assignee,
        "updated": issue.updated,
        "fields": values,
        "more": more,
        "content_trust": "untrusted",
    }


# --------------------------------------------------------------------------- update


def issue_update(
    client: ServiceClient,
    jira: JiraConfig,
    key: str,
    assignments: list[Assignment],
    users: UserLookup,
) -> dict[str, Any]:
    editmeta = get_editmeta(client, key)
    checks: list[Check] = []
    if not editmeta:
        checks.append(
            Check(
                "editable",
                "fail",
                f"Jira shows no editable fields on {key} for your account.",
                f"Ask a Jira admin for edit permission on {key}.",
            )
        )
    aliases = jira.aliases(key.rpartition("-")[0])
    covered: dict[str, PlannedField] = {}
    planned_fields: list[PlannedField] = []
    for assignment in assignments:
        planned, check = plan_field(
            assignment,
            editmeta,
            aliases,
            users,
            covered,
            scope=f"on the edit screen of {key}",
            reserved={k: v for k, v in SYSTEM_FIELDS.items() if k != "summary"},
        )
        checks.append(check)
        if planned is not None:
            planned_fields.append(planned)
    if failed(checks):
        raise refusal(f"Updating {key}", checks)

    client.announce(f"Updating {key}: {', '.join(f.name for f in planned_fields)}")
    ids = [f.id for f in planned_fields]
    inspect = _issue_cmd("issue", "get", key, "--fields", ",".join(ids))
    fix = (
        f"Do not retry yet. Read the fields back with `{inspect}` and compare them with what you "
        "sent; only if they differ, run the update again. yojo does not retry writes."
    )
    try:
        client.request(
            "PUT",
            f"/rest/api/2/issue/{seg(key)}",
            json_body={"fields": {f.id: f.payload for f in planned_fields}},
        )
    except YojoError as exc:
        if exc.code == "outcome_unknown":
            exc.fix = fix
        raise
    try:
        after = get_issue(client, key, ids)
    except YojoError as exc:
        raise client.unknown_outcome(
            f"Jira accepted the update but reading it back failed ({exc.code})", fix
        ) from None
    differs = [
        f.name
        for f in planned_fields
        if not confirmed(editmeta[f.id], f.payload, after.values.get(f.id))
    ]
    if differs:
        error = client.unknown_outcome(
            "Jira accepted the update but reports different values for "
            + ", ".join(differs)
            + " (a workflow or automation may have changed them)",
            fix,
        )
        error.details["fields"] = differs
        raise error
    return {
        "key": after.key,
        "url": browse_url(client, after.key),
        "status": after.status,
        "changed": [f.as_dict() for f in planned_fields],
    }


# --------------------------------------------------------------------------- link


def _link_candidates(types: list[LinkType]) -> list[Candidate]:
    pool: list[Candidate] = []
    for link in types:
        pool.append(Candidate(label=link.name, value=(link, "outward"), names=(link.outward,)))
        if link.inward.casefold() != link.outward.casefold():
            pool.append(Candidate(label=link.inward, value=(link, "inward")))
    return pool


def _already_linked(raw_links: Any, link: LinkType, outward_key: str) -> bool:
    if not isinstance(raw_links, list):
        return False
    for item in raw_links:
        if not isinstance(item, dict):
            continue
        same_type = isinstance(item.get("type"), dict) and item["type"].get("id") == link.id
        if same_type and nested_str(item.get("outwardIssue"), "key") == outward_key:
            return True
    return False


def issue_link(client: ServiceClient, key: str, other: str, type_name: str) -> dict[str, Any]:
    checks: list[Check] = []
    if key == other:
        checks.append(
            Check(
                "issues", "fail", "An issue cannot be linked to itself.", "Pass two different keys."
            )
        )
    source, check = _issue_exists(client, key, f"issue:{key}", ("issuelinks",))
    checks.append(check)
    target, check = _issue_exists(client, other, f"issue:{other}", ("issuelinks",))
    checks.append(check)
    types = link_types(client)
    chosen: tuple[LinkType, str] | None = None
    try:
        chosen = pick(type_name, _link_candidates(types), "link type").value
    except Unresolved as exc:
        names = "|".join(t.name for t in types[:MAX_LISTED])
        edit = Edit("flag", "--type", exc.replacement or placeholder(names))
        checks.append(Check("link_type", "fail", exc.message, exc.fix, exc.details(), edit))
    if chosen is not None:
        link, direction = chosen
        relation = link.outward if direction == "outward" else link.inward
        checks.append(Check("link_type", "pass", f"{key} {relation} {other}."))
    if source is not None:
        checks.append(_permission_check(client, key, "LINK_ISSUES", "link issues"))
    if chosen is not None and source is not None and target is not None:
        link, direction = chosen
        outward_from, outward_to = (key, other) if direction == "outward" else (other, key)
        current = source if direction == "outward" else target
        if _already_linked(current.values.get("issuelinks"), link, outward_to):
            checks.append(
                Check(
                    "existing_link",
                    "fail",
                    f"{outward_from} already {link.outward} {outward_to}.",
                    "Nothing to do; the link exists.",
                )
            )
    if failed(checks) or chosen is None:
        raise refusal(f"Linking {key} and {other}", checks)

    link, direction = chosen
    outward_from, outward_to = (key, other) if direction == "outward" else (other, key)

    relation = link.outward if direction == "outward" else link.inward
    client.announce(f"Linking {key} {relation} {other}")
    inspect = _issue_cmd("issue", "get", outward_from, "--fields", "issuelinks")
    fix = (
        f"Do not retry yet. Check the links with `{inspect}`; only if {outward_from} does not "
        f"{link.outward} {outward_to}, run the link again. yojo does not retry writes."
    )
    body = {
        "type": {"name": link.name},
        # Jira's link source is the inward issue: it reads "<inwardIssue> <outward wording>".
        "inwardIssue": {"key": outward_from},
        "outwardIssue": {"key": outward_to},
    }
    try:
        client.request("POST", "/rest/api/2/issueLink", json_body=body)
    except YojoError as exc:
        if exc.code == "outcome_unknown":
            exc.fix = fix
        raise
    try:
        after = get_issue(client, outward_from, ["issuelinks"])
    except YojoError as exc:
        raise client.unknown_outcome(f"reading the link back failed ({exc.code})", fix) from None
    if not _already_linked(after.values.get("issuelinks"), link, outward_to):
        raise client.unknown_outcome("Jira accepted the link but it is not on the issue", fix)
    return {
        "key": key,
        "url": browse_url(client, key),
        "relation": relation,
        "other": other,
        "link_type": link.name,
    }


# --------------------------------------------------------------------------- comment


def comment_add(client: ServiceClient, key: str, body: str) -> dict[str, Any]:
    checks: list[Check] = []
    if not body.strip():
        checks.append(
            Check(
                "body",
                "fail",
                "The comment body is empty.",
                "Pass the comment text with --body 'text' or --body-file FILE.",
                retry=Edit("flag", "--body", placeholder("comment")),
            )
        )
    issue, check = _issue_exists(client, key)
    checks.append(check)
    if issue is not None:
        checks.append(_permission_check(client, key, "ADD_COMMENTS", "add comments"))
    if failed(checks):
        raise refusal(f"Commenting on {key}", checks)
    client.announce(f"Adding a comment to {key} ({len(body)} characters)")
    view = browse_url(client, key)
    fix = (
        f"Do not retry yet. Open {view} and look for a comment whose text matches what you sent; "
        "only if there is none, add it again. yojo does not retry writes."
    )
    try:
        result = client.request(
            "POST", f"/rest/api/2/issue/{seg(key)}/comment", json_body={"body": body}
        )
    except YojoError as exc:
        if exc.code == "outcome_unknown":
            exc.fix = fix
        raise
    comment_id = result.get("id") if isinstance(result, dict) else None
    if not isinstance(comment_id, str) or not comment_id.strip():
        raise client.unknown_outcome("comment response did not include a comment id", fix)
    return {
        "key": key,
        "url": client.url(
            f"/browse/{seg(key)}?focusedCommentId={seg(comment_id)}#comment-{seg(comment_id)}"
        ),
        "comment_id": comment_id,
    }


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


def _checked_size(path: Path, info: os.stat_result, limit: int) -> int:
    """Return the size of a regular file within ``limit``, or raise a file error."""
    if not stat.S_ISREG(info.st_mode):
        raise YojoError(
            "file_invalid",
            f"Not a regular file: {path}",
            "Pass --file with the path to a regular file.",
            {"path": str(path)},
        )
    if info.st_size > limit:
        raise YojoError(
            "attachment_too_large",
            f"{path.name} is {info.st_size} bytes; jira.max_attachment_bytes is {limit}.",
            "Attach a smaller file (compress or split it), or raise jira.max_attachment_bytes in "
            "yojo.yaml if the team agrees.",
            {"size": info.st_size, "limit": limit},
        )
    return info.st_size


def _file_error(path: Path, message: str, fix: str = "Check file permissions.") -> YojoError:
    return YojoError("file_invalid", message, fix, {"path": str(path)})


def _same_file(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _unchanged(a: os.stat_result, b: os.stat_result) -> bool:
    return _same_file(a, b) and (a.st_size, a.st_mtime_ns, a.st_ctime_ns) == (
        b.st_size,
        b.st_mtime_ns,
        b.st_ctime_ns,
    )


def _open_checked(path: Path, limit: int) -> tuple[int, os.stat_result]:
    """Open ``path`` without blocking and return the descriptor of the regular file checked."""
    try:
        info = path.stat()
    except OSError:
        raise _file_error(
            path,
            f"File not found or unreadable: {path}",
            "Pass --file with the path to an existing regular file.",
        ) from None
    _checked_size(path, info, limit)
    # O_NONBLOCK keeps open() from waiting on a FIFO swapped in after stat(); fstat() rejects it.
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise _file_error(path, f"Cannot open {path}.") from None
    try:
        opened = os.fstat(fd)
        _checked_size(path, opened, limit)
        if not _same_file(info, opened):
            raise _file_error(
                path,
                f"{path} was replaced between the check and the open; nothing was sent.",
                "Wait until the file is fully written, then retry.",
            )
    except OSError:
        os.close(fd)
        raise _file_error(path, f"Cannot read {path}.") from None
    except YojoError:
        os.close(fd)
        raise
    return fd, opened


def _snapshot(path: Path, fd: int, opened: os.stat_result, snapshot: BinaryIO) -> None:
    """Copy the opened file into ``snapshot``, failing if the source changes meanwhile."""
    try:
        copied = 0
        while copied <= opened.st_size:
            chunk = os.read(fd, min(UPLOAD_CHUNK, opened.st_size + 1 - copied))
            if not chunk:
                break
            snapshot.write(chunk)
            copied += len(chunk)
        changed = copied != opened.st_size or not _unchanged(opened, os.fstat(fd))
        snapshot.seek(0)
    except OSError:
        raise _file_error(path, f"Cannot read {path}.") from None
    if changed:
        raise _file_error(
            path,
            f"{path.name} changed while yojo was reading it; nothing was sent.",
            "Wait until the file is fully written, then retry.",
        )


def _confirmed_attachments(
    client: ServiceClient, key: str, result: Any, name: str, size: int
) -> list[dict[str, Any]]:
    """Return the attachments Jira confirmed; anything incomplete means the outcome is unknown."""

    def unknown(reason: str) -> YojoError:
        return client.unknown_outcome(
            reason,
            f"Open {key} in the Jira issue view and check whether an attachment named {name!r} "
            f"of {size} bytes was added before retrying `yojo jira attachment add`. yojo does "
            "not retry writes.",
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


def _attachment_checks(client: ServiceClient, key: str, name: str, size: int) -> list[Check]:
    checks: list[Check] = []
    issue, check = _issue_exists(client, key)
    checks.append(check)
    if issue is not None:
        checks.append(_permission_check(client, key, "CREATE_ATTACHMENTS", "attach files"))
    enabled, limit = attachment_settings(client)
    if enabled is False:
        checks.append(
            Check(
                "attachments_enabled",
                "fail",
                "Attachments are turned off in this Jira.",
                "Ask a Jira admin to enable attachments.",
            )
        )
    elif enabled is None:
        checks.append(
            Check(
                "attachments_enabled",
                "unverified",
                "Jira did not report whether attachments are enabled.",
                "Run the command again; if it repeats, ask a Jira admin to check "
                "/rest/api/2/attachment/meta.",
            )
        )
    elif limit is not None and size > limit:
        checks.append(
            Check(
                "server_limit",
                "fail",
                f"{name} is {size} bytes; this Jira accepts at most {limit}.",
                "Attach a smaller file (compress or split it).",
                {"size": size, "limit": limit},
            )
        )
    else:
        checks.append(
            Check("attachments_enabled", "pass", "Jira accepts attachments of this size.")
        )
    return checks


def attachment_add(client: ServiceClient, jira: JiraConfig, key: str, path: Path) -> dict[str, Any]:
    fd, opened = _open_checked(path, jira.max_attachment_bytes)
    size = opened.st_size
    name = os.path.basename(path)
    try:
        checks = _attachment_checks(client, key, name, size)
    except BaseException:
        os.close(fd)
        raise
    if failed(checks):
        os.close(fd)
        raise refusal(f"Attaching {name} to {key}", checks)
    boundary = secrets.token_hex(16)
    # SECURITY-REVIEW: the bytes are uploaded from an owner-only temporary snapshot (unnamed on
    # POSIX, delete-on-close on Windows), bounded by jira.max_attachment_bytes, so what is sent is
    # exactly what was checked even if the source is rewritten during the upload.
    with os.fdopen(fd, "rb", buffering=0) as source, tempfile.TemporaryFile() as handle:
        # Windows typeshed models TemporaryFile as a wrapper; its file methods satisfy BinaryIO.
        snapshot = cast(BinaryIO, handle)
        _snapshot(path, source.fileno(), opened, snapshot)
        body, length = _multipart(snapshot, size, name, boundary)
        client.announce(f"Attaching {name} ({size} bytes) to {key}")
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
            # The request may already be on the wire, so the server's handling is unknown.
            error = client.unknown_outcome(
                f"file {change} while uploading",
                f"{path.name} {change} while uploading; yojo aborted the body before completion "
                f"and never sent more than {size} bytes of it. Open {key} in the Jira "
                f"issue view and look for an attachment named {sanitize_text(name, 120)!r} "
                "(complete or partial) before retrying; remove a partial one. Wait until the file "
                "is fully written before any retry. yojo does not retry writes.",
            )
            error.details.update(
                {"path": str(path), "declared_size": size, "dispatched": "aborted"}
            )
            raise error from None
    attachments = _confirmed_attachments(client, key, result, name, size)
    return {"key": key, "url": browse_url(client, key), "attachments": attachments}


# --------------------------------------------------------------------------- search


def search(client: ServiceClient, jql: str, start: int, limit: int) -> dict[str, Any]:
    raw = client.get(
        "/rest/api/2/search",
        {"jql": jql, "startAt": start, "maxResults": limit, "fields": SEARCH_FIELDS},
    )
    if not isinstance(raw, dict) or not isinstance(raw.get("issues"), list):
        raise YojoError(
            "response_invalid",
            "Jira returned an unexpected search response.",
            "Check that the Jira URL points at Jira Data Center.",
        )
    consumed = raw["issues"][:limit]
    issues = []
    for item in consumed:
        if not isinstance(item, dict):
            continue
        fields = item.get("fields")
        if not isinstance(fields, dict):
            raise YojoError(
                "response_invalid",
                "Jira returned a search result without a fields object.",
                "Check that the Jira URL points at Jira Data Center.",
                {"what": "search issue fields"},
            )
        issues.append(
            {
                "key": str(item.get("key", "")),
                "summary": fields.get("summary")
                if isinstance(fields.get("summary"), str)
                else None,
                "status": nested_str(fields.get("status")),
                "issue_type": nested_str(fields.get("issuetype")),
                "assignee": nested_str(fields.get("assignee"), "displayName"),
            }
        )
    raw_total = raw.get("total")
    end = start + len(consumed)
    total = raw_total if isinstance(raw_total, int) else end
    next_start = end if consumed and end < total else None
    return {
        "start": start,
        "limit": limit,
        "total": total,
        "next_start": next_start,
        "issues": issues,
        "content_trust": "untrusted",
    }


# --------------------------------------------------------------------------- config check


def _drift(check_id: str, message: str, fix: str, details: dict[str, Any] | None = None) -> Check:
    return Check(check_id, "drift", message, fix, details or {})


def _alias_checks(
    prefix: str, aliases: dict[str, FieldRef], index: dict[str, FieldMeta], where: str
) -> list[Check]:
    checks: list[Check] = []
    for friendly, ref in aliases.items():
        check_id = f"{prefix}field:{friendly}"
        if ref.id is not None:
            meta = index.get(ref.id)
            if meta is None:
                checks.append(
                    _drift(
                        check_id,
                        f"Alias {friendly!r} points at {ref.id}, which Jira does not have.",
                        f"Fix {where}.{friendly}.id in yojo.yaml.",
                    )
                )
            else:
                checks.append(Check(check_id, "pass", f"{friendly!r} is {meta.shown}."))
            continue
        assert ref.jira_name is not None
        try:
            meta = resolve(ref.jira_name, index, {})
        except Unresolved as exc:
            checks.append(
                _drift(
                    check_id,
                    f"Alias {friendly!r}: {exc.message}",
                    f"Fix {where}.{friendly} in yojo.yaml. {exc.fix}",
                    exc.details(),
                )
            )
            continue
        checks.append(Check(check_id, "pass", f"{friendly!r} is {meta.shown}."))
    return checks


def _project_statuses(client: ServiceClient, key: str) -> set[str] | None:
    raw = client.get(f"/rest/api/2/project/{seg(key)}/statuses")
    if not isinstance(raw, list):
        return None
    names: set[str] = set()
    for item in raw:
        statuses = item.get("statuses") if isinstance(item, dict) else None
        for status in statuses if isinstance(statuses, list) else []:
            name = nested_str(status)
            if name:
                names.add(name)
    return names or None


def _name_check(check_id: str, name: str, known: set[str], what: str, fix: str) -> Check:
    if name.casefold() in {k.casefold() for k in known}:
        return Check(check_id, "pass", f"{what} {name!r} exists.")
    pool = [Candidate(label=k, value=k) for k in sorted(known)]
    try:
        pick(name, pool, what)
    except Unresolved as exc:
        return _drift(check_id, exc.message, f"{fix} {exc.fix}", exc.details())
    return Check(check_id, "pass", f"{what} {name!r} exists.")  # pragma: no cover


def _project_checks(
    client: ServiceClient, project: ProjectConfig, index: dict[str, FieldMeta], jira: JiraConfig
) -> list[Check]:
    prefix = f"{project.key}:"
    where = f"jira.projects[{project.key}]"
    try:
        statuses = _project_statuses(client, project.key)
    except YojoError as exc:
        if exc.code not in ("not_found", "forbidden"):
            raise
        return [
            _drift(
                f"{prefix}project",
                f"Project {project.key} was not found or is not visible to you.",
                f"Fix the key in {where}, or ask a Jira admin for access to {project.key}.",
            )
        ]
    checks = _alias_checks(prefix, project.fields, index, f"{where}.fields")
    aliases = jira.aliases(project.key)
    if statuses is None:
        checks.append(
            Check(
                f"{prefix}statuses",
                "unverified",
                f"Jira did not return the statuses of {project.key}.",
                "Run `yojo config check` again; if it repeats, check project permissions.",
            )
        )
        statuses = set()
    for rule in project.transitions.values():
        r_prefix = f"{prefix}transition:{rule.name}"
        if statuses:
            fix = f"Fix transitions.{rule.name} in {where}."
            for source in rule.from_statuses:
                checks.append(
                    _name_check(f"{r_prefix}:from:{source}", source, statuses, "status", fix)
                )
            checks.append(_name_check(f"{r_prefix}:to", rule.to, statuses, "status", fix))
        names = [*rule.auto_set, *(r.field for r in rule.require if r.field)]
        for name in names:
            try:
                meta = resolve(name, index, aliases)
            except Unresolved as exc:
                checks.append(
                    _drift(
                        f"{r_prefix}:field:{name}",
                        exc.message,
                        f"Fix transitions.{rule.name} in {where}. {exc.fix}",
                        exc.details(),
                    )
                )
                continue
            checks.append(Check(f"{r_prefix}:field:{name}", "pass", f"{name!r} is {meta.shown}."))
    if project.create_required:
        types = {t.name for t in create_issue_types(client, project.key)}
        for type_name, required in project.create_required.items():
            c_prefix = f"{prefix}create:{type_name}"
            checks.append(
                _name_check(
                    c_prefix, type_name, types, "issue type", f"Fix create.{type_name} in {where}."
                )
            )
            for name in required:
                try:
                    resolve(name, index, aliases)
                except Unresolved as exc:
                    checks.append(
                        _drift(
                            f"{c_prefix}:field:{name}",
                            exc.message,
                            f"Fix create.{type_name}.required in {where}. {exc.fix}",
                            exc.details(),
                        )
                    )
                    continue
                checks.append(Check(f"{c_prefix}:field:{name}", "pass", f"{name!r} exists."))
    return checks


def config_check(client: ServiceClient, config: Config, jira: JiraConfig) -> list[Check]:
    try:
        index = all_fields(client)
    except YojoError as exc:
        if exc.code != "metadata_incomplete":
            raise
        return [Check("fields", "unverified", exc.message, exc.fix)]
    checks = _alias_checks("", jira.fields, index, "jira.fields")
    for project in jira.projects.values():
        checks.extend(_project_checks(client, project, index, jira))
    if not checks:
        checks.append(
            Check("config", "pass", f"{config.path.name} has no Jira aliases or rules to check.")
        )
    return checks
