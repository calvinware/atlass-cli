"""Bitbucket Data Center repositories, branches, and pull requests via REST 1.0.

Every list is bounded and pages with Bitbucket's own ``nextPageStart`` cursor; malformed paging
fails closed. Every write checks everything it can first and refuses with all problems at once;
a success is returned only when Bitbucket's response confirms the pull request or comment that
was sent. Approving and merging are left to people and have no command here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from yojo.contracts import YojoError
from yojo.resolve import Candidate, Unresolved, pick, suggest
from yojo.response import TextBudget, nested_str
from yojo.retry import POSIX_NOTE, PROGRAM, Edit, placeholder, posix
from yojo.transport import ServiceClient, sanitize_text, seg
from yojo.workflow import Check, failed, refusal

API = "/rest/api/1.0"
BRANCH_PREFIX = "refs/heads/"
# Preflight scans read at most this many pages of SCAN_LIMIT items; a longer list is unverified.
MAX_SCAN_PAGES = 10
SCAN_LIMIT = 100
MAX_LIST_TITLE = 60
# Encoded bytes all titles in one pull request list may use, so a page stays small.
LIST_TITLE_BYTES = 1000
MAX_TITLE_CHARS = 255
MAX_DIFF_FILES = 10
MAX_REVIEWERS = 10
DIFF_CONTEXT_LINES = 3
SEGMENT_PREFIX = {"ADDED": "+", "REMOVED": "-", "CONTEXT": " "}
# Separators str.splitlines() breaks on besides \n and \r (which a source line may not contain).
LINE_BREAKS = re.compile("[\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")


@dataclass(frozen=True)
class Repo:
    """A repository as ``PROJECT/slug``."""

    project: str
    slug: str

    @property
    def ref(self) -> str:
        return f"{self.project}/{self.slug}"

    @property
    def api(self) -> str:
        return f"{API}/projects/{seg(self.project)}/repos/{seg(self.slug)}"

    @property
    def web(self) -> str:
        return f"/projects/{seg(self.project)}/repos/{seg(self.slug)}"


def parse_repo(text: str) -> Repo:
    project, _, slug = text.partition("/")
    return Repo(project, slug)


def _cmd(*args: str) -> list[str]:
    return [PROGRAM, "bitbucket", *args]


def _invalid(what: str) -> YojoError:
    return YojoError(
        "response_invalid",
        f"Bitbucket returned an unexpected {what} response.",
        "Check that the Bitbucket URL (--base-url or bitbucket.base_url) points at Bitbucket Data "
        "Center including any context path.",
        {"what": what},
    )


def _pr_url(client: ServiceClient, repo: Repo, pr_id: int, tail: str = "") -> str:
    return client.url(f"{repo.web}/pull-requests/{pr_id}{tail}")


def _positive_int(value: Any) -> int | None:
    return value if type(value) is int and value >= 1 else None


def _when(millis: Any) -> str | None:
    if type(millis) is not int or millis < 0:
        return None
    try:
        moment = datetime.fromtimestamp(millis / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_of(raw: Any) -> Repo | None:
    if not isinstance(raw, dict):
        return None
    slug = raw.get("slug")
    key = nested_str(raw.get("project"), "key")
    if not isinstance(slug, str) or not slug or not key:
        return None
    return Repo(key, slug)


def _same_repo(a: Repo | None, b: Repo) -> bool:
    return a is not None and a.slug == b.slug and a.project.casefold() == b.project.casefold()


def _ref_id(pr: Any, side: str) -> str | None:
    ref = pr.get(side) if isinstance(pr, dict) else None
    return nested_str(ref, "id")


def _ref_repo(pr: Any, side: str) -> Repo | None:
    ref = pr.get(side) if isinstance(pr, dict) else None
    return _repo_of(ref.get("repository")) if isinstance(ref, dict) else None


def _branch_name(ref_id: str | None) -> str | None:
    if ref_id is None:
        return None
    return ref_id.removeprefix(BRANCH_PREFIX)


# --------------------------------------------------------------------------- paging


def _page(
    client: ServiceClient,
    path: str,
    params: dict[str, Any],
    start: int,
    limit: int,
    what: str,
) -> tuple[list[Any], int | None]:
    """One page and the server's cursor for the next one (None on the last page).

    The next cursor is only ever Bitbucket's ``nextPageStart``; a page that is not the last but
    has a missing, repeating, or backward cursor is refused rather than guessed.
    """
    raw = client.get(path, {**params, "start": start, "limit": limit})
    if not isinstance(raw, dict) or not isinstance(raw.get("values"), list):
        raise _invalid(what)
    values: list[Any] = raw["values"]
    last = raw.get("isLastPage")
    echoed = raw.get("start", start)
    if type(last) is not bool or len(values) > limit or echoed != start:
        raise _invalid(f"{what} page")
    if last:
        return values, None
    cursor = raw.get("nextPageStart")
    if type(cursor) is not int or cursor <= start:
        raise YojoError(
            "response_invalid",
            f"Bitbucket returned a {what} page without a usable next cursor, so the list cannot "
            "continue safely.",
            "Run the command again. If it repeats, narrow it (a smaller --limit, or --filter for "
            "branches) and check that no proxy rewrites Bitbucket responses.",
            {"what": f"{what} page", "start": start},
        )
    return values, cursor


@dataclass
class Scan:
    """A bounded walk over every page; ``complete`` is False when it stopped early or an item
    could not be read."""

    found: Any = None
    complete: bool = False
    seen: list[Any] = field(default_factory=list)
    problem: str | None = None


def _scan(
    client: ServiceClient,
    path: str,
    params: dict[str, Any],
    what: str,
    match: Callable[[Any], bool],
    valid: Callable[[Any], bool] | None = None,
) -> Scan:
    scan = Scan()
    start = 0
    unreadable = 0
    for _ in range(MAX_SCAN_PAGES):
        try:
            values, cursor = _page(client, path, params, start, SCAN_LIMIT, what)
        except YojoError as exc:
            if exc.code != "response_invalid":
                raise
            scan.problem = exc.message
            return scan
        for value in values:
            scan.seen.append(value)
            if valid is not None and not valid(value):
                unreadable += 1
                continue
            if match(value):
                scan.found = value
                scan.complete = True
                return scan
        if cursor is None:
            if unreadable:
                scan.problem = (
                    f"Bitbucket returned {unreadable} {what} item(s) without the details needed "
                    "to compare them"
                )
                return scan
            scan.complete = True
            return scan
        start = cursor
    scan.problem = f"more than {MAX_SCAN_PAGES * SCAN_LIMIT} items to check"
    return scan


def _listing(
    data: dict[str, Any], start: int, limit: int, cursor: int | None, key: str, rows: list[Any]
) -> dict[str, Any]:
    return {
        **data,
        "start": start,
        "limit": limit,
        "next_start": cursor,
        key: rows,
        "content_trust": "untrusted",
    }


# --------------------------------------------------------------------------- identity


def application_version(client: ServiceClient) -> str:
    """Confirm the server is Bitbucket; ``application-properties`` needs no login."""
    props = client.get(f"{API}/application-properties")
    name = nested_str(props, "displayName")
    version = nested_str(props, "version")
    if name is None or "bitbucket" not in name.casefold() or version is None:
        raise YojoError(
            "response_invalid",
            f"The server at {client.base_url} did not identify itself as Bitbucket Data Center.",
            "Pass the Bitbucket application root including any context path, for example "
            "--base-url https://bitbucket.example.com/bitbucket.",
            {"what": "application properties"},
        )
    return sanitize_text(version, 40)


def confirm_authenticated(client: ServiceClient) -> None:
    """Call an endpoint that answers only for a signed-in user (401 otherwise)."""
    recent = client.get(f"{API}/profile/recent/repos", {"limit": 1})
    if not isinstance(recent, dict) or not isinstance(recent.get("values"), list):
        raise YojoError(
            "auth_failed",
            "Bitbucket did not confirm an authenticated user for this credential.",
            "Check the token and run `yojo login --service bitbucket` again.",
            {"service": "bitbucket"},
        )


def verify_login(client: ServiceClient) -> str:
    """Both checks must pass before a token is stored; returns the Bitbucket version."""
    version = application_version(client)
    confirm_authenticated(client)
    return version


def config_check(client: ServiceClient, config_path: str) -> list[Check]:
    try:
        version = application_version(client)
    except YojoError as exc:
        if exc.code == "response_invalid":
            return [
                Check(
                    "bitbucket:application",
                    "drift",
                    exc.message,
                    f"Edit {config_path}: set bitbucket.base_url to the Bitbucket Data Center "
                    "root including any context path.",
                )
            ]
        return [Check("bitbucket:application", "unverified", exc.message, exc.fix)]
    checks = [
        Check(
            "bitbucket:application",
            "pass",
            f"Bitbucket Data Center {version} answers at {client.base_url}.",
        )
    ]
    try:
        confirm_authenticated(client)
    except YojoError as exc:
        checks.append(Check("bitbucket:auth", "unverified", exc.message, exc.fix))
    else:
        checks.append(Check("bitbucket:auth", "pass", "Bitbucket accepts the stored token."))
    return checks


# --------------------------------------------------------------------------- repositories


def _repo_row(client: ServiceClient, raw: Any) -> dict[str, Any]:
    repo = _repo_of(raw)
    if repo is None:
        raise _invalid("repository")
    return {
        "repo": repo.ref,
        "name": nested_str(raw, "name") or repo.slug,
        "url": client.url(f"{repo.web}/browse"),
    }


def repo_list(client: ServiceClient, project: str | None, start: int, limit: int) -> dict[str, Any]:
    path = f"{API}/projects/{seg(project)}/repos" if project else f"{API}/repos"
    values, cursor = _page(client, path, {}, start, limit, "repository list")
    rows = [_repo_row(client, v) for v in values]
    return _listing({"project": project}, start, limit, cursor, "repos", rows)


def _get_repo(client: ServiceClient, repo: Repo) -> tuple[Repo, dict[str, Any]]:
    raw = client.get(repo.api)
    found = _repo_of(raw)
    if found is None:
        raise _invalid("repository")
    if not _same_repo(found, repo):
        raise YojoError(
            "response_invalid",
            f"Bitbucket returned {sanitize_text(found.ref, 100)} when {repo.ref} was requested.",
            "Check that the Bitbucket URL is not routed through a rewriting proxy.",
            {"what": "repository", "requested": repo.ref},
        )
    assert isinstance(raw, dict)
    return found, raw


def _clone(raw: dict[str, Any], kind: str) -> str | None:
    links = raw.get("links")
    clones = links.get("clone") if isinstance(links, dict) else None
    for item in clones if isinstance(clones, list) else []:
        if nested_str(item, "name") == kind and nested_str(item, "href"):
            return sanitize_text(item["href"], 500)
    return None


def _default_branch(client: ServiceClient, repo: Repo) -> str | None:
    try:
        raw = client.get(f"{repo.api}/branches/default")
    except YojoError as exc:
        if exc.code in ("not_found", "response_invalid"):
            return None
        raise
    return nested_str(raw, "displayId")


def repo_get(client: ServiceClient, ref: str) -> dict[str, Any]:
    repo, raw = _get_repo(client, parse_repo(ref))
    return {
        "repo": repo.ref,
        "name": nested_str(raw, "name") or repo.slug,
        "state": nested_str(raw, "state"),
        "public": raw.get("public") is True,
        "default_branch": _default_branch(client, repo),
        "clone_http": _clone(raw, "http"),
        "clone_ssh": _clone(raw, "ssh"),
        "url": client.url(f"{repo.web}/browse"),
        "content_trust": "untrusted",
    }


# --------------------------------------------------------------------------- branches


def _branch_ok(raw: Any) -> bool:
    name = nested_str(raw, "displayId")
    return bool(name) and nested_str(raw, "id") == BRANCH_PREFIX + str(name)


def branch_list(
    client: ServiceClient, ref: str, text: str | None, start: int, limit: int
) -> dict[str, Any]:
    repo = parse_repo(ref)
    params: dict[str, Any] = {"details": "false"}
    if text:
        params["filterText"] = text
    values, cursor = _page(client, f"{repo.api}/branches", params, start, limit, "branch list")
    rows = []
    for raw in values:
        if not _branch_ok(raw):
            raise _invalid("branch")
        rows.append(
            {
                "name": raw["displayId"],
                "default": raw.get("isDefault") is True,
                "commit": nested_str(raw, "latestCommit"),
            }
        )
    data = {"repo": repo.ref, "filter": text}
    listing = _listing(data, start, limit, cursor, "branches", rows)
    listing["url"] = client.url(f"{repo.web}/branches")
    return listing


@dataclass(frozen=True)
class Branch:
    id: str
    name: str


def _resolve_branch(
    client: ServiceClient, repo: Repo, given: str, flag: str, origin: str
) -> tuple[Branch | None, Check]:
    """Find the one branch named ``given`` (exact case wins; otherwise a unique any-case match)."""
    check_id = flag.lstrip("-")
    name = given.strip().removeprefix(BRANCH_PREFIX)
    scan = _scan(
        client,
        f"{repo.api}/branches",
        {"filterText": name, "details": "false"},
        "branch list",
        lambda raw: raw["displayId"] == name,
        _branch_ok,
    )
    listing = posix(_cmd("branch", "list", "--repo", repo.ref, "--filter", name))
    pool = [
        Candidate(label=raw["displayId"], value=raw, token=raw["displayId"])
        for raw in scan.seen
        if _branch_ok(raw)
    ]
    unverified = Check(
        check_id,
        "unverified",
        f"Could not confirm branch {sanitize_text(name, 100)!r} in {repo.ref}"
        + (f": {scan.problem}" if scan.problem else "."),
        f"Check the branch name with `{listing}` and pass its exact name.",
    )
    try:
        chosen = pick(name, pool, "branch", f"in {repo.ref}")
    except Unresolved as exc:
        if not scan.complete and exc.kind == "not_found":
            return None, unverified
        if exc.kind == "not_found" and not pool:
            exc = _near_miss(client, repo, name, exc)
        edit = Edit("flag", flag, exc.replacement or placeholder("branch"), origin)
        return None, Check(
            check_id,
            "fail",
            exc.message,
            f"{exc.fix} List branches with `{listing}`.",
            exc.details(),
            edit,
        )
    if not scan.complete:
        return None, unverified
    branch = Branch(chosen.value["id"], chosen.value["displayId"])
    return branch, Check(check_id, "pass", f"Branch {branch.name!r} exists in {repo.ref}.")


def _near_miss(client: ServiceClient, repo: Repo, name: str, exc: Unresolved) -> Unresolved:
    """The name filter found nothing (often a typo): suggest from one page of recent branches."""
    try:
        values, _ = _page(
            client,
            f"{repo.api}/branches",
            {"details": "false", "orderBy": "MODIFICATION"},
            0,
            SCAN_LIMIT,
            "branch list",
        )
    except YojoError as error:
        if error.code != "response_invalid":
            raise
        return exc
    pool = [
        Candidate(label=raw["displayId"], value=raw, token=raw["displayId"])
        for raw in values
        if _branch_ok(raw)
    ]
    return Unresolved("not_found", "branch", name, pool, suggest(name, pool), exc.scope)


# --------------------------------------------------------------------------- pull requests


def _pr_row(raw: Any, titles: TextBudget | None = None) -> dict[str, Any]:
    pr_id = _positive_int(raw.get("id")) if isinstance(raw, dict) else None
    if pr_id is None:
        raise _invalid("pull request")
    title = nested_str(raw, "title") or ""
    if titles is not None:
        cut = len(title) > MAX_LIST_TITLE
        title, trimmed = titles.fit(title[: MAX_LIST_TITLE - 3] if cut else title)
        if cut or trimmed:
            title += "..."
    author = raw.get("author")
    return {
        "id": pr_id,
        "title": title,
        "state": nested_str(raw, "state"),
        "source": _branch_name(_ref_id(raw, "fromRef")),
        "target": _branch_name(_ref_id(raw, "toRef")),
        "author": _person(author.get("user") if isinstance(author, dict) else None),
        "updated": _when(raw.get("updatedDate")),
    }


def _person(user: Any) -> str | None:
    return nested_str(user, "displayName") or nested_str(user, "name")


def pr_list(client: ServiceClient, ref: str, state: str, start: int, limit: int) -> dict[str, Any]:
    repo = parse_repo(ref)
    values, cursor = _page(
        client, f"{repo.api}/pull-requests", {"state": state}, start, limit, "pull request list"
    )
    titles = TextBudget(False, LIST_TITLE_BYTES)
    rows = [_pr_row(raw, titles) for raw in values]
    listing = _listing(
        {"repo": repo.ref, "state": state}, start, limit, cursor, "pull_requests", rows
    )
    listing["url"] = client.url(f"{repo.web}/pull-requests")
    return listing


def _get_pr(client: ServiceClient, repo: Repo, pr_id: int) -> dict[str, Any]:
    raw = client.get(f"{repo.api}/pull-requests/{pr_id}")
    if not isinstance(raw, dict) or _positive_int(raw.get("id")) is None:
        raise _invalid("pull request")
    if raw["id"] != pr_id or not _same_repo(_ref_repo(raw, "toRef"), repo):
        raise YojoError(
            "response_invalid",
            f"Bitbucket returned a different pull request when #{pr_id} in {repo.ref} was "
            "requested.",
            "Check that the Bitbucket URL is not routed through a rewriting proxy.",
            {"what": "pull request", "requested_id": pr_id},
        )
    return raw


def pr_get(client: ServiceClient, ref: str, pr_id: int, full: bool = False) -> dict[str, Any]:
    repo = parse_repo(ref)
    raw = _get_pr(client, repo, pr_id)
    row = _pr_row(raw)
    target_repo = _ref_repo(raw, "toRef") or repo
    reviewers = []
    for item in (raw.get("reviewers") or [])[:MAX_REVIEWERS]:
        if isinstance(item, dict):
            reviewers.append(
                {"name": _person(item.get("user")), "status": nested_str(item, "status")}
            )
    description = nested_str(raw, "description") or ""
    shown, truncated = TextBudget(full).fit(description)
    return {
        "id": pr_id,
        "repo": target_repo.ref,
        "title": nested_str(raw, "title") or "",
        "state": row["state"],
        "source": row["source"],
        "target": row["target"],
        "author": row["author"],
        "created": _when(raw.get("createdDate")),
        "updated": row["updated"],
        "reviewers": reviewers,
        "description": shown,
        "truncated": truncated,
        "more": posix(_cmd("pr", "get", str(pr_id), "--repo", target_repo.ref, "--full"))
        if truncated
        else None,
        "url": _pr_url(client, target_repo, pr_id),
        "content_trust": "untrusted",
    }


# --------------------------------------------------------------------------- diff


def _diff_path(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    text = raw.get("toString")
    if isinstance(text, str) and text:
        return text
    parts = raw.get("components")
    if isinstance(parts, list) and parts and all(isinstance(p, str) for p in parts):
        return "/".join(parts)
    return None


def _number(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _cut(raw: dict[str, Any]) -> bool:
    """Bitbucket documents ``truncated`` as a JSON boolean or the string "true" or "false"."""
    value = raw.get("truncated")
    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise _invalid("diff")


def _escape(ch: str) -> str:
    if ch in '"\\':
        return "\\" + ch
    if ch.isprintable():
        return ch
    named = {"\n": "\\n", "\t": "\\t", "\r": "\\r"}
    if ch in named:
        return named[ch]
    code = ord(ch)
    if code < 0x100:
        return f"\\x{code:02x}"
    return f"\\u{code:04x}" if code < 0x10000 else f"\\U{code:08x}"


def _header(prefix: str, path: str) -> str:
    """Quote a header path the way Git does, so a newline in a file name cannot forge diff lines."""
    name = prefix + path
    if name.isprintable() and '"' not in name and "\\" not in name:
        return name
    return '"' + "".join(_escape(ch) for ch in name) + '"'


def _diff_file(raw: Any) -> tuple[dict[str, Any], list[str], bool]:
    """One file's row, its unified-diff lines, and whether Bitbucket cut any of it."""
    if not isinstance(raw, dict):
        raise _invalid("diff")
    old, new = _diff_path(raw.get("source")), _diff_path(raw.get("destination"))
    if old is None and new is None:
        raise _invalid("diff")
    change = (
        "added"
        if old is None
        else "deleted"
        if new is None
        else "renamed"
        if old != new
        else "modified"
    )
    lines = [
        f"diff --git {_header('a/', old or new or '')} {_header('b/', new or old or '')}",
        f"--- {_header('a/', old)}" if old else "--- /dev/null",
        f"+++ {_header('b/', new)}" if new else "+++ /dev/null",
    ]
    if raw.get("binary") is True:
        lines.append("Binary files differ")
    cut = _cut(raw)
    added = removed = 0
    hunks = raw.get("hunks", [])
    if not isinstance(hunks, list):
        raise _invalid("diff")
    for hunk in hunks:
        segments = hunk.get("segments") if isinstance(hunk, dict) else None
        if not isinstance(segments, list):
            raise _invalid("diff")
        cut = _cut(hunk) or cut
        lines.append(
            f"@@ -{_number(hunk.get('sourceLine'))},{_number(hunk.get('sourceSpan'))} "
            f"+{_number(hunk.get('destinationLine'))},{_number(hunk.get('destinationSpan'))} @@"
        )
        for segment in segments:
            prefix = SEGMENT_PREFIX.get(nested_str(segment, "type") or "")
            seg_lines = segment.get("lines") if prefix is not None else None
            if prefix is None or not isinstance(seg_lines, list):
                raise _invalid("diff")
            cut = _cut(segment) or cut
            for line in seg_lines:
                text = nested_str(line, "line")
                if text is None or "\n" in text or "\r" in text:
                    raise _invalid("diff")
                cut = _cut(line) or cut
                lines.append(prefix + LINE_BREAKS.sub(lambda m: _escape(m.group()), text))
                added += prefix == "+"
                removed += prefix == "-"
    row = {
        "path": new or old,
        "old_path": old if change == "renamed" else None,
        "change": change,
        "added": added,
        "removed": removed,
    }
    return row, lines, cut


def _path_argv(row: dict[str, Any]) -> list[str] | None:
    """The --path (and --old-path) flags for one file, or None if the flag would refuse it."""
    names = [row["path"], *([row["old_path"]] if row["old_path"] else [])]
    if not all(name.isprintable() for name in names):
        return None
    return ["--path", row["path"], *(["--old-path", row["old_path"]] if row["old_path"] else [])]


def _git_inspect(raw: dict[str, Any], rows: list[dict[str, Any]] | None) -> str | None:
    """A local Git command for the changes Bitbucket would not return, if both commits are known."""
    from_hash, to_hash = raw.get("fromHash"), raw.get("toHash")
    if not (
        isinstance(from_hash, str)
        and re.fullmatch(r"[0-9a-f]{7,64}", from_hash)
        and isinstance(to_hash, str)
        and re.fullmatch(r"[0-9a-f]{7,64}", to_hash)
    ):
        return None
    paths: list[str] = []
    for row in rows or []:
        paths += [*([row["old_path"]] if row["old_path"] else []), row["path"]]
    if not all(path.isprintable() for path in paths):
        paths = []
    return posix(["git", "diff", from_hash, to_hash, *(["--", *paths] if paths else [])])


def pr_diff(
    client: ServiceClient,
    ref: str,
    pr_id: int,
    path: str | None,
    full: bool = False,
    old_path: str | None = None,
) -> dict[str, Any]:
    repo = parse_repo(ref)
    if old_path is not None and path is None:
        raise YojoError(
            "usage_error",
            "--old-path needs --path.",
            "Pass the file's current name with --path and its name before the change with "
            "--old-path.",
        )
    endpoint = f"{repo.api}/pull-requests/{pr_id}/diff"
    if path is not None:
        endpoint += "/" + "/".join(seg(part) for part in path.split("/"))
    base = _cmd("pr", "diff", str(pr_id), "--repo", repo.ref)
    anchor = "#" + "/".join(seg(part) for part in path.split("/")) if path else ""
    web = _pr_url(client, repo, pr_id, "/diff" + anchor)
    params: dict[str, Any] = {"contextLines": DIFF_CONTEXT_LINES, "withComments": "false"}
    if old_path is not None:
        params["srcPath"] = old_path
    try:
        raw = client.get(endpoint, params)
    except YojoError as exc:
        if exc.code == "response_invalid" and "max_bytes" in exc.details:
            exc.message = f"The diff of pull request #{pr_id} is too large to fetch at once."
            exc.fix = (
                "Fetch one file at a time: "
                f"`{posix([*base, '--path', placeholder('file path')])}`. The changed files are "
                f"listed at {web}."
            )
        raise
    if not isinstance(raw, dict) or not isinstance(raw.get("diffs"), list):
        raise _invalid("diff")
    rows: list[dict[str, Any]] = []
    text_lines: list[str] = []
    cut_rows: list[dict[str, Any]] = []
    for item in raw["diffs"]:
        row, lines, cut = _diff_file(item)
        if path is not None and (
            row["path"] != path or (old_path is not None and row["old_path"] != old_path)
        ):
            raise _invalid("diff")
        rows.append(row)
        text_lines += lines
        if cut:
            cut_rows.append(row)
    if path is not None and not rows:
        listing = posix(base)
        raise YojoError(
            "not_found",
            f"Pull request #{pr_id} has no changes to {sanitize_text(path, 200)!r}.",
            f"Run `{listing}` to list the changed files. For a renamed file pass its new name "
            "with --path and its old name with --old-path.",
            {"path": path},
        )
    top_cut = _cut(raw)
    server_cut = top_cut or bool(cut_rows)
    text = "\n".join(text_lines) + ("\n" if text_lines else "")
    shown, cut_here = TextBudget(full).fit(text)
    if cut_here and "\n" in shown:
        shown = shown[: shown.rindex("\n") + 1]
    listed = rows if full else rows[:MAX_DIFF_FILES]
    truncated = cut_here or len(listed) < len(rows)
    more: str | None = None
    if truncated:
        selected = ["--path", path, *(["--old-path", old_path] if old_path else [])] if path else []
        more = posix([*base, *selected, "--full"])
    elif server_cut and path is None and cut_rows:
        flags = _path_argv(cut_rows[0])
        more = posix([*base, *flags, "--full"]) if flags else None
    inspect: str | None = None
    if server_cut:
        scope = None if top_cut and path is None else rows if path else cut_rows
        inspect = _git_inspect(raw, scope)
    return {
        "id": pr_id,
        "repo": repo.ref,
        "path": path,
        "files_total": len(rows),
        "files": listed,
        "diff": shown,
        "diff_chars": len(text),
        "truncated": truncated,
        "server_truncated": server_cut,
        "truncated_files": [row["path"] for row in cut_rows][:MAX_DIFF_FILES],
        "complete": not truncated and not server_cut,
        "more": more,
        "inspect": inspect,
        "url": web,
        "content_trust": "untrusted",
    }


# --------------------------------------------------------------------------- create


@dataclass
class PullRequestRequest:
    repo: str | None = None
    source: str | None = None
    target: str | None = None
    title: str | None = None
    description: str | None = None
    # Where each value came from: flag, input, or missing. Input values get no retry line.
    origins: dict[str, str] = field(default_factory=dict)

    def origin(self, name: str) -> str:
        return self.origins.get(name, "flag")


def _title_check(request: PullRequestRequest) -> Check:
    title = request.title
    edit = Edit("flag", "--title", placeholder("title"), request.origin("title"))
    if title is None or not title.strip():
        return Check(
            "title",
            "fail",
            "The title is missing or blank.",
            "Add --title 'one-line title'.",
            retry=edit,
        )
    if len(title) > MAX_TITLE_CHARS:
        return Check(
            "title",
            "fail",
            f"The title is longer than {MAX_TITLE_CHARS} characters.",
            "Shorten --title to one line; put detail in --description.",
            {"max_chars": MAX_TITLE_CHARS},
            edit,
        )
    return Check("title", "pass", "The title is set.")


def _missing(name: str, flag: str, what: str, example: str) -> Check:
    return Check(
        name,
        "fail",
        f"The {what} is missing.",
        f"Add {flag} {example}.",
        retry=Edit("flag", flag, placeholder(example), "missing"),
    )


def _local_checks(request: PullRequestRequest) -> list[Check]:
    checks = [_title_check(request)]
    if request.repo is None:
        checks.append(_missing("repo", "--repo", "repository", "PROJECT/slug"))
    return checks


def _waiting(name: str, flag: str, what: str, value: str | None) -> Check:
    if value is None:
        return _missing(name, flag, what, "branch")
    return Check(
        name,
        "unverified",
        f"The {what} cannot be checked until the repository is known.",
        "Resolve the repo check first.",
    )


def _duplicate_waiting() -> Check:
    return Check(
        "duplicate",
        "unverified",
        "Open pull requests cannot be checked until the repository and both branches are known.",
        "Resolve the repo, source, and target checks first.",
    )


def _duplicate_check(client: ServiceClient, repo: Repo, source: Branch, target: Branch) -> Check:
    def same_pair(pr: Any) -> bool:
        return (
            _ref_id(pr, "fromRef") == source.id
            and _ref_id(pr, "toRef") == target.id
            and _same_repo(_ref_repo(pr, "fromRef"), repo)
            and _same_repo(_ref_repo(pr, "toRef"), repo)
        )

    def readable(pr: Any) -> bool:
        return None not in (
            _ref_id(pr, "fromRef"),
            _ref_id(pr, "toRef"),
            _ref_repo(pr, "fromRef"),
            _ref_repo(pr, "toRef"),
        )

    scan = _scan(
        client,
        f"{repo.api}/pull-requests",
        {"state": "OPEN", "at": source.id, "direction": "OUTGOING"},
        "pull request list",
        same_pair,
        readable,
    )
    if scan.found is not None:
        pr_id = scan.found.get("id")
        shown = str(pr_id) if _positive_int(pr_id) else placeholder("id")
        return Check(
            "duplicate",
            "fail",
            f"Pull request #{shown} already proposes {source.name!r} into {target.name!r} in "
            f"{repo.ref}.",
            f"Use the open pull request: `{posix(_cmd('pr', 'get', shown, '--repo', repo.ref))}`. "
            "To add to it, push to the branch or comment on it.",
            {"pull_request": pr_id if _positive_int(pr_id) else None},
            Edit("none", ""),
        )
    if not scan.complete:
        listing = posix(_cmd("pr", "list", "--repo", repo.ref))
        return Check(
            "duplicate",
            "unverified",
            f"Could not confirm there is no open pull request from {source.name!r} into "
            f"{target.name!r}: {scan.problem}.",
            f"Run the command again. If it repeats, look for one with `{listing}`; yojo does not "
            "open a pull request it cannot check for duplicates.",
        )
    return Check(
        "duplicate", "pass", f"No open pull request from {source.name!r} into {target.name!r}."
    )


def pull_request_refusal(request: PullRequestRequest) -> YojoError:
    """Refuse a create that names no repository; nothing remote can be checked."""
    checks = _local_checks(request)
    checks.append(_waiting("source", "--source", "source branch", request.source))
    checks.append(_waiting("target", "--target", "target branch", request.target))
    checks.append(_duplicate_waiting())
    return refusal("Opening a pull request", checks)


def pr_create(client: ServiceClient, request: PullRequestRequest) -> dict[str, Any]:
    if request.repo is None:
        raise pull_request_refusal(request)
    checks = _local_checks(request)
    given = parse_repo(request.repo)
    repo: Repo | None = None
    try:
        repo, _ = _get_repo(client, given)
    except YojoError as exc:
        if exc.code not in ("not_found", "forbidden"):
            raise
        listing = posix(_cmd("repo", "list", "--project", given.project))
        checks.append(
            Check(
                "repo",
                "fail",
                f"Repository {given.ref} was not found or you cannot see it.",
                f"Check PROJECT/slug; `{listing}` lists the repositories you can see.",
                retry=Edit("flag", "--repo", placeholder("PROJECT/slug"), request.origin("repo")),
            )
        )
    else:
        checks.append(Check("repo", "pass", f"Repository {repo.ref} is visible to you."))

    branches: dict[str, Branch | None] = {"source": None, "target": None}
    for name, what in (("source", "source branch"), ("target", "target branch")):
        value = getattr(request, name)
        if repo is None or value is None:
            checks.append(_waiting(name, f"--{name}", what, value))
            continue
        branch, check = _resolve_branch(client, repo, value, f"--{name}", request.origin(name))
        branches[name] = branch
        checks.append(check)

    source, target = branches["source"], branches["target"]
    if repo is not None and source is not None and target is not None:
        if source.id == target.id:
            checks.append(
                Check(
                    "same_branch",
                    "fail",
                    f"The source and target are both {source.name!r}.",
                    "Pass a different --target, usually the repository's default branch.",
                    retry=Edit(
                        "flag", "--target", placeholder("target branch"), request.origin("target")
                    ),
                )
            )
        else:
            checks.append(_duplicate_check(client, repo, source, target))
    else:
        checks.append(_duplicate_waiting())

    if failed(checks):
        raise refusal(f"Opening a pull request in {repo.ref if repo else given.ref}", checks)
    assert repo is not None and source is not None and target is not None
    title = request.title or ""
    return _create(client, repo, source, target, title, request.description)


def _ref_body(branch: Branch, repo: Repo) -> dict[str, Any]:
    return {
        "id": branch.id,
        "repository": {"slug": repo.slug, "project": {"key": repo.project}},
    }


def _create_recovery(
    client: ServiceClient,
    reason: str,
    repo: Repo,
    source: Branch,
    target: Branch,
    title: str,
    pr_id: int | None = None,
) -> YojoError:
    argv = {"list": _cmd("pr", "list", "--repo", repo.ref, "--state", "ALL")}
    if pr_id is not None:
        argv["get"] = _cmd("pr", "get", str(pr_id), "--repo", repo.ref)
    error = client.unknown_outcome(
        reason,
        f"Do not retry yet. List pull requests in every state with `{posix(argv['list'])}`, "
        "following next_start through every page, and look for one from "
        f"{source.name!r} into {target.name!r} titled {sanitize_text(title, 120)!r}"
        + (f"; also check `{posix(argv['get'])}`" if "get" in argv else "")
        + ". A first page without it does not show the create failed. Run the create again only "
        "when the full list has no match; if unsure, ask a person to check the repository. "
        f"yojo does not retry writes. {POSIX_NOTE} Use details.recovery_argv for the argv lists.",
    )
    error.details["recovery_argv"] = argv
    return error


def _create(
    client: ServiceClient,
    repo: Repo,
    source: Branch,
    target: Branch,
    title: str,
    description: str | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": title,
        "fromRef": _ref_body(source, repo),
        "toRef": _ref_body(target, repo),
    }
    if description is not None:
        body["description"] = description
    client.announce(
        f"Opening a pull request in {repo.ref}: {source.name} into {target.name}, "
        f"{sanitize_text(title, 60)!r}"
    )
    try:
        created = client.request("POST", f"{repo.api}/pull-requests", json_body=body)
    except YojoError as exc:
        if exc.code == "outcome_unknown":
            recovery = _create_recovery(
                client, str(exc.details.get("reason", "")), repo, source, target, title
            )
            exc.fix = recovery.fix
            exc.details["recovery_argv"] = recovery.details["recovery_argv"]
        raise
    reason = _create_mismatch(created, repo, source, target, title, description)
    if reason is not None:
        pr_id = _positive_int(created.get("id")) if isinstance(created, dict) else None
        raise _create_recovery(client, reason, repo, source, target, title, pr_id)
    assert isinstance(created, dict)
    pr_id = created["id"]
    return {
        "id": pr_id,
        "repo": repo.ref,
        "title": created["title"],
        "source": source.name,
        "target": target.name,
        "state": nested_str(created, "state"),
        "url": _pr_url(client, repo, pr_id),
    }


def _text_confirmed(submitted: str, returned: Any) -> bool:
    return isinstance(returned, str) and returned == submitted


def _create_mismatch(
    created: Any,
    repo: Repo,
    source: Branch,
    target: Branch,
    title: str,
    description: str | None,
) -> str | None:
    """Return why a create response does not confirm the submitted pull request, or None."""
    if not isinstance(created, dict):
        return "create response was not a JSON object"
    if _positive_int(created.get("id")) is None:
        return "create response did not include a valid pull request id"
    if _ref_id(created, "fromRef") != source.id or not _same_repo(
        _ref_repo(created, "fromRef"), repo
    ):
        return "create response did not confirm the source branch and repository"
    if _ref_id(created, "toRef") != target.id or not _same_repo(_ref_repo(created, "toRef"), repo):
        return "create response did not confirm the target branch and repository"
    if not _text_confirmed(title, created.get("title")):
        return "create response did not confirm the submitted title"
    returned = created.get("description")
    if description == "" and returned in (None, ""):
        return None
    if description is not None and not _text_confirmed(description, returned):
        return "create response did not confirm the submitted description"
    return None


# --------------------------------------------------------------------------- comment


def _duplicate_comment(client: ServiceClient, repo: Repo, pr_id: int, body: str) -> Check:
    def readable(activity: Any) -> bool:
        if not isinstance(activity, dict) or not isinstance(activity.get("action"), str):
            return False
        return activity["action"] != "COMMENTED" or (
            nested_str(activity.get("comment"), "text") is not None
        )

    def same_text(activity: Any) -> bool:
        return activity["action"] == "COMMENTED" and _text_confirmed(
            body, activity["comment"]["text"]
        )

    scan = _scan(
        client,
        f"{repo.api}/pull-requests/{pr_id}/activities",
        {},
        "activity list",
        same_text,
        readable,
    )
    view = _pr_url(client, repo, pr_id, "/overview")
    if scan.found is not None:
        comment_id = _positive_int(scan.found["comment"].get("id"))
        return Check(
            "duplicate",
            "fail",
            f"Pull request #{pr_id} already has a comment with exactly this text.",
            f"Nothing to send if it is yours (see {view}); to add something new, change the text.",
            {"comment_id": comment_id},
            Edit("none", ""),
        )
    if not scan.complete:
        return Check(
            "duplicate",
            "unverified",
            f"Could not confirm pull request #{pr_id} has no identical comment: {scan.problem}.",
            f"Run the command again. If it repeats, check {view} by hand; yojo does not post a "
            "comment it cannot check for duplicates.",
        )
    return Check("duplicate", "pass", "No identical comment is on the pull request.")


def pr_comment(
    client: ServiceClient, ref: str, pr_id: int, body: str, origin: str = "flag"
) -> dict[str, Any]:
    repo = parse_repo(ref)
    checks: list[Check] = []
    blank = not body.strip()
    if blank:
        checks.append(
            Check(
                "body",
                "fail",
                "The comment is empty.",
                "Pass the comment text with --body 'text' or --body-file FILE.",
                retry=Edit("flag", "--body", placeholder("comment"))
                if origin == "flag"
                else Edit("none", ""),
            )
        )
    try:
        pr = _get_pr(client, repo, pr_id)
    except YojoError as exc:
        if exc.code not in ("not_found", "forbidden"):
            raise
        listing = posix(_cmd("pr", "list", "--repo", repo.ref, "--state", "ALL"))
        checks.append(
            Check(
                "pull_request",
                "fail",
                f"Pull request #{pr_id} was not found in {repo.ref} or you cannot see it.",
                f"Check the ID and repository; `{listing}` lists its pull requests.",
                retry=Edit("none", ""),
            )
        )
        raise refusal(f"Commenting on pull request #{pr_id}", checks) from None
    repo = _ref_repo(pr, "toRef") or repo
    state = nested_str(pr, "state") or "unknown"
    checks.append(Check("pull_request", "pass", f"Pull request #{pr_id} is {state.lower()}."))
    if not blank:
        checks.append(_duplicate_comment(client, repo, pr_id, body))
    if failed(checks):
        raise refusal(f"Commenting on pull request #{pr_id}", checks)

    view = _pr_url(client, repo, pr_id, "/overview")
    argv = {"pull_request": _cmd("pr", "get", str(pr_id), "--repo", repo.ref)}
    fix = (
        f"Do not retry yet. Open {view} and look for a comment matching what you sent; post "
        "again only if it is not there. Do not send the same comment from two calls at once: "
        "each checks for duplicates before posting, so both can post. yojo does not retry writes."
    )
    client.announce(f"Commenting on pull request #{pr_id} in {repo.ref} ({len(body)} characters)")
    try:
        result = client.request(
            "POST", f"{repo.api}/pull-requests/{pr_id}/comments", json_body={"text": body}
        )
    except YojoError as exc:
        if exc.code == "outcome_unknown":
            exc.fix = fix
            exc.details["recovery_argv"] = argv
        raise
    comment_id = _positive_int(result.get("id")) if isinstance(result, dict) else None
    if comment_id is None or not _text_confirmed(body, result.get("text")):
        error = client.unknown_outcome(
            "comment response did not confirm the comment id and text", fix
        )
        error.details["recovery_argv"] = argv
        raise error
    return {
        "id": pr_id,
        "repo": repo.ref,
        "comment_id": comment_id,
        "url": _pr_url(client, repo, pr_id, f"/overview?commentId={comment_id}"),
    }
