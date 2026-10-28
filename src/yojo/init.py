"""`yojo init`: draft a version 1 ``yojo.yaml`` from live metadata, then preview or save it.

The draft holds only what the caller named (service URLs, project keys) and any team policy given
as ``--input``. Every selection is confirmed against the live service with the stored login before
anything is written, and the file is saved only by explicit request, never over a file that
changed since its preview. No credential ever enters the draft.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from yojo import bitbucket, files, jira, retry
from yojo.config import (
    CONFIG_FILENAME,
    LEGACY_FILENAME,
    MAX_CONFIG_BYTES,
    ServiceConfig,
    StrictSafeLoader,
    legacy_config,
    parse_config_file,
    url_problem,
    validate_config,
)
from yojo.contracts import YojoError
from yojo.transport import ServiceClient, sanitize_text, seg

OpenClient = Callable[[str, ServiceConfig], AbstractContextManager[ServiceClient]]
VerifyUser = Callable[[ServiceClient, str], str | None]

HEADER = "# Drafted by `yojo init` from live metadata. Review it, then run `yojo config check`.\n"
PASSING = ("pass", "not_applicable")
READ = (retry.PROGRAM, "--json")


@dataclass(frozen=True)
class InitRequest:
    jira_url: str | None = None
    projects: tuple[str, ...] = ()
    confluence_url: str | None = None
    bitbucket_url: str | None = None
    ca_bundle: str | None = None
    repos: tuple[str, ...] = ()
    input: str | None = None
    output: str | None = None
    save: bool = False
    replace: bool = False
    expected_sha256: str | None = None

    def argv(self, *extra: str) -> list[str]:
        out = [retry.PROGRAM, "--json", "init"]
        if self.jira_url:
            out += ["--jira-url", self.jira_url]
        for key in self.projects:
            out += ["--project", key]
        if self.confluence_url:
            out += ["--confluence-url", self.confluence_url]
        if self.bitbucket_url:
            out += ["--bitbucket-url", self.bitbucket_url]
        if self.ca_bundle:
            out += ["--ca-bundle", self.ca_bundle]
        for ref in self.repos:
            out += ["--repo", ref]
        if self.input is not None:
            out += ["--input", self.input]
        if self.output is not None:
            out += ["--output", self.output]
        return out + list(extra)


def _usage(message: str, fix: str) -> YojoError:
    return YojoError("usage_error", message, fix)


def _url(flag: str, value: str | None) -> str | None:
    if value is None:
        return None
    normalized, problem = url_problem(value)
    if normalized is None:
        raise YojoError(
            "usage_error",
            f"{flag} {problem}; nothing was written.",
            f"Pass the HTTPS application root including any context path, for example {flag} "
            "https://jira.example.com/jira.",
            {"flag": flag, "problem": problem},
        )
    return normalized


def normalize(request: InitRequest) -> InitRequest:
    """Check how the flags fit together and normalize the URLs; nothing is contacted yet."""
    example = "yojo --json init --jira-url https://jira.example.com/jira --project PROJ"
    if not (request.jira_url or request.confluence_url or request.bitbucket_url):
        raise _usage(
            "yojo init needs at least one service URL; nothing was written.",
            f"Pass --jira-url, --confluence-url, or --bitbucket-url, for example `{example}`.",
        )
    if request.projects and not request.jira_url:
        raise _usage("--project needs --jira-url.", f"Add --jira-url, for example `{example}`.")
    if request.repos and not request.bitbucket_url:
        raise _usage(
            "--repo needs --bitbucket-url.",
            "Add --bitbucket-url, for example `yojo --json init --bitbucket-url "
            "https://bitbucket.example.com/bitbucket --repo PROJ/web`.",
        )
    if request.input is not None and not request.jira_url:
        raise _usage(
            "--input holds Jira aliases and rules, so it needs --jira-url.", "Add --jira-url."
        )
    if (request.replace or request.expected_sha256 is not None) and not (
        request.save and request.replace and request.expected_sha256 is not None
    ):
        raise _usage(
            "--replace and --expected-sha256 go together, with --save.",
            "Preview first, then add `--save --replace --expected-sha256 HEX` using the "
            "preview's existing_sha256.",
        )
    return InitRequest(
        jira_url=_url("--jira-url", request.jira_url),
        projects=tuple(dict.fromkeys(request.projects)),
        confluence_url=_url("--confluence-url", request.confluence_url),
        bitbucket_url=_url("--bitbucket-url", request.bitbucket_url),
        ca_bundle=request.ca_bundle,
        repos=tuple(dict.fromkeys(request.repos)),
        input=request.input,
        output=request.output,
        save=request.save,
        replace=request.replace,
        expected_sha256=request.expected_sha256,
    )


# --------------------------------------------------------------------------- draft


def _policy_projects(request: InitRequest, policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for entry in policy.get("projects", []):
        key = entry["key"]
        if key in entries:
            raise YojoError(
                "input_invalid",
                f"The --input policy lists project {key} twice; nothing was written.",
                f"Merge the two jira.projects entries for {key} into one.",
                {"project": key},
            )
        if key not in request.projects:
            raise YojoError(
                "input_invalid",
                f"The --input policy has rules for project {key}, which is not a --project; "
                "nothing was written.",
                f"Add `--project {key}`, or remove {key} from jira.projects in the input.",
                {"project": key, "projects": list(request.projects)},
            )
        entries[key] = {k: v for k, v in entry.items() if k != "key"}
    return entries


def document(request: InitRequest, policy: dict[str, Any] | None) -> dict[str, Any]:
    doc: dict[str, Any] = {"version": 1}
    if request.jira_url:
        section: dict[str, Any] = {"base_url": request.jira_url}
        if request.ca_bundle:
            section["ca_bundle"] = request.ca_bundle
        rules = (policy or {}).get("jira", {})
        if "fields" in rules:
            section["fields"] = rules["fields"]
        entries = _policy_projects(request, rules)
        if request.projects:
            section["projects"] = [{"key": k, **entries.get(k, {})} for k in request.projects]
        doc["jira"] = section
    if request.confluence_url:
        doc["confluence"] = {"base_url": request.confluence_url}
        if request.ca_bundle:
            doc["confluence"]["ca_bundle"] = request.ca_bundle
    if request.bitbucket_url:
        doc["bitbucket"] = {"base_url": request.bitbucket_url}
        if request.ca_bundle:
            doc["bitbucket"]["ca_bundle"] = request.ca_bundle
    return doc


def render_yaml(doc: dict[str, Any], dest: Path) -> str:
    """Serialize the draft and prove it reads back unchanged through the normal loader."""
    try:
        validate_config(doc, dest)
    except YojoError as exc:
        if exc.code != "config_invalid":
            raise
        where = exc.details.get("where")
        fix = (
            "Pass --ca-bundle with a readable PEM file, relative to --output or as an "
            "absolute path."
            if isinstance(where, str) and where.endswith(".ca_bundle")
            else (
                f"Correct {where or 'the policy'} in the --input document; "
                "see docs/configuration.md."
            )
        )
        raise YojoError(
            "input_invalid",
            f"The draft would not be a valid yojo.yaml: {exc.details.get('problem')}"
            + (f" at {where}" if where else "")
            + "; nothing was written.",
            fix,
            {"where": where, "problem": exc.details.get("problem")},
        ) from None
    text = HEADER + yaml.safe_dump(
        doc, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100
    )
    if yaml.load(text, Loader=StrictSafeLoader) != doc:  # noqa: S506 - SafeLoader subclass
        raise YojoError(
            "input_invalid",
            "A value in the --input policy cannot be written to YAML exactly; nothing was written.",
            "Use plain strings, lists, and true/false in the policy.",
        )
    if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise YojoError(
            "input_invalid",
            f"The draft exceeds the {MAX_CONFIG_BYTES}-byte config limit; nothing was written.",
            "Reduce the --input policy so the generated yojo.yaml fits the config limit.",
            {"max_bytes": MAX_CONFIG_BYTES},
        )
    return text


# --------------------------------------------------------------------------- live checks


def _not_visible(what: str, flag: str, value: str, service: str) -> YojoError:
    return YojoError(
        "not_found",
        f"{service} {what} {value} was not found or is not visible to you; nothing was written.",
        f"Check the {what} and correct or drop `{flag} {value}`, or ask a {service} admin "
        "for access.",
        {"service": service.lower(), what: value},
    )


def _jira_project(client: ServiceClient, key: str) -> dict[str, str]:
    try:
        raw = client.get(f"/rest/api/2/project/{seg(key)}")
    except YojoError as exc:
        if exc.code == "not_found":
            raise _not_visible("project", "--project", key, "Jira") from None
        raise
    name = raw.get("name") if isinstance(raw, dict) else None
    if not isinstance(raw, dict) or raw.get("key") != key or not isinstance(name, str) or not name:
        raise YojoError(
            "response_invalid",
            f"Jira returned an unexpected answer for project {key}; nothing was written.",
            "Check that --jira-url points at Jira Data Center including any context path.",
            {"what": "project", "project": key},
        )
    return {"key": key, "name": sanitize_text(name, 255)}


def _policy_problems(client: ServiceClient, doc: dict[str, Any], dest: Path) -> None:
    config = validate_config(doc, dest)
    assert config.jira is not None
    checks = jira.config_check(client, config, config.jira)
    problems = [c.as_dict() for c in checks if c.status not in PASSING]
    if problems:
        raise YojoError(
            "input_invalid",
            f"{len(problems)} name(s) in the --input policy do not match live Jira; nothing was "
            "written.",
            "Apply each problem's fix to the --input document, then run the same command again.",
            {"problems": problems},
        )


def _bitbucket_repo(client: ServiceClient, ref: str) -> dict[str, str]:
    try:
        data = bitbucket.repo_get(client, ref)
    except YojoError as exc:
        if exc.code == "not_found":
            raise _not_visible("repository", "--repo", ref, "Bitbucket") from None
        raise
    return {"repo": data["repo"], "name": data["name"]}


def verify(
    request: InitRequest,
    doc: dict[str, Any],
    dest: Path,
    open_client: OpenClient,
    verify_user: VerifyUser,
) -> dict[str, list[dict[str, Any]]]:
    config = validate_config(doc, dest)
    services: list[dict[str, Any]] = []
    projects: list[dict[str, str]] = []
    repos: list[dict[str, str]] = []
    if request.jira_url:
        assert config.jira is not None
        with open_client("jira", config.jira) as client:
            user = verify_user(client, "jira")
            services.append(
                {"service": "jira", "base_url": request.jira_url, "user": user, "version": None}
            )
            projects = [_jira_project(client, key) for key in request.projects]
            if request.input is not None:
                _policy_problems(client, doc, dest)
    if request.confluence_url:
        assert config.confluence is not None
        confluence_cfg = config.confluence
        with open_client("confluence", confluence_cfg) as client:
            user = verify_user(client, "confluence")
            services.append(
                {
                    "service": "confluence",
                    "base_url": request.confluence_url,
                    "user": user,
                    "version": None,
                }
            )
    if request.bitbucket_url:
        assert config.bitbucket is not None
        bitbucket_cfg = config.bitbucket
        with open_client("bitbucket", bitbucket_cfg) as client:
            version = bitbucket.verify_login(client)
            services.append(
                {
                    "service": "bitbucket",
                    "base_url": request.bitbucket_url,
                    "user": None,
                    "version": version,
                }
            )
            repos = [_bitbucket_repo(client, ref) for ref in request.repos]
    return {"services": services, "projects": projects, "repos": repos}


# --------------------------------------------------------------------------- save


def _save(request: InitRequest, dest: Path, current: bytes | None, data: bytes) -> str:
    existing = files.sha256(current) if current is not None else None
    if current is None:
        if request.replace:
            raise files.conflict(
                dest,
                f"--replace was given, but {dest} does not exist; nothing was written.",
                f"Run `{retry.posix(request.argv('--save'))}` to create it.",
            )
        legacy = dest.parent / LEGACY_FILENAME
        if dest.name == CONFIG_FILENAME and legacy.is_file():
            raise legacy_config(legacy)
        files.publish(dest, data, None)
        return "created"
    if request.replace and request.expected_sha256 != existing:
        raise files.conflict(
            dest,
            f"{dest} changed since the preview (its sha256 is not --expected-sha256); nothing "
            "was written.",
            f"Run the preview again (`{retry.posix(request.argv())}`), review it, and pass its "
            "existing_sha256.",
            existing_sha256=existing,
            expected_sha256=request.expected_sha256,
        )
    if current == data:
        return "unchanged"
    if not request.replace:
        replace = request.argv("--save", "--replace", "--expected-sha256", str(existing))
        raise files.conflict(
            dest,
            f"{dest} already exists and differs from the draft; nothing was written.",
            f"Review the preview (`{retry.posix(request.argv())}`). To replace the file, run "
            f"`{retry.posix(replace)}`.",
            existing_sha256=existing,
            sha256=files.sha256(data),
        )
    files.publish(dest, data, current)
    return "replaced"


def _next_steps(
    request: InitRequest,
    action: str,
    current: bytes | None,
    existing: str | None,
    dest: Path,
    cwd: Path,
) -> list[str]:
    if action == "preview":
        extra = ["--save"]
        if current is not None:
            extra += ["--replace", "--expected-sha256", str(existing)]
        return [retry.posix(request.argv(*extra))]
    # Discovery from the working directory finds only ./yojo.yaml (or a parent's); name any other.
    config = [] if dest == files.resolve(cwd, CONFIG_FILENAME) else ["--config", str(dest)]
    if request.projects:
        jql = f"project = {request.projects[0]} ORDER BY updated DESC"
        read = ["jira", "search", "--jql", jql, "--limit", "5"]
    elif request.repos:
        read = ["bitbucket", "pr", "list", "--repo", request.repos[0]]
    elif request.bitbucket_url:
        read = ["bitbucket", "repo", "list", "--limit", "5"]
    elif request.confluence_url:
        read = ["confluence", "page", "search", "--cql", "type = page", "--limit", "5"]
    else:
        read = ["jira", "search", "--jql", "assignee = currentUser()", "--limit", "5"]
    return [
        retry.posix([*READ, "config", "check", *config]),
        "yojo --json agent install --target agents",
        retry.posix([*READ, *read, *config]),
    ]


def draft(
    request: InitRequest,
    policy: dict[str, Any] | None,
    cwd: Path,
    open_client: OpenClient,
    verify_user: VerifyUser,
) -> dict[str, Any]:
    dest = files.resolve(cwd, request.output or CONFIG_FILENAME)
    doc = document(request, policy)
    text = render_yaml(doc, dest)
    data = text.encode("utf-8")
    try:
        observed = verify(request, doc, dest, open_client, verify_user)
    except YojoError as exc:
        if exc.code != "auth_missing" or not request.ca_bundle:
            raise
        service = exc.details.get("service")
        base_url = exc.details.get("base_url")
        if not isinstance(service, str) or not isinstance(base_url, str):
            raise
        # login resolves --ca-bundle against the working directory, init against --output.
        bundle = Path(request.ca_bundle).expanduser()
        login = retry.posix(
            [
                retry.PROGRAM,
                "--json",
                "login",
                "--service",
                service,
                "--base-url",
                base_url,
                "--ca-bundle",
                str(files.resolve(dest.parent, str(bundle))),
            ]
        )
        raise YojoError(
            exc.code,
            exc.message,
            f"Ask a human to run `{login}` in a terminal and enter the token at the hidden prompt.",
            exc.details,
        ) from None
    files.refuse_symlinks(dest)
    current = files.read_current(dest)
    existing = files.sha256(current) if current is not None else None
    action = _save(request, dest, current, data) if request.save else "preview"
    if action in ("created", "replaced"):
        parse_config_file(dest)
    notes = []
    if action == "preview":
        notes.append("Nothing was written. Review the YAML, then run the next command to save it.")
    if request.input is None:
        notes.append(
            "No team aliases or rules were added; yojo never infers them. Pass them with --input "
            "(see docs/configuration.md)."
        )
    if request.repos:
        notes.append(
            "Bitbucket repositories were checked but are not saved: version 1 of yojo.yaml has no "
            "repository list. Pass --repo PROJECT/slug to each bitbucket command."
        )
    return {
        "action": action,
        "path": str(dest),
        "yaml": text,
        "sha256": files.sha256(data),
        "existing_sha256": existing,
        **observed,
        "notes": notes,
        "next": _next_steps(request, action, current, existing, dest, cwd),
        "content_trust": "untrusted",
    }
