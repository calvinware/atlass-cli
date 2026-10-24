"""Entry point: argument parsing, dispatch, and the single JSON output boundary."""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, NoReturn, TextIO

import httpx
from jsonschema import Draft202012Validator, ValidationError

from atlass_cli import __version__, confluence, features, jira, workflow
from atlass_cli.auth import (
    Credential,
    CredentialStore,
    default_store,
    validate_secret,
    validate_username,
)
from atlass_cli.config import (
    Config,
    ServiceConfig,
    load_config,
    require_confluence,
    require_jira,
    require_project,
)
from atlass_cli.contracts import (
    COMMANDS,
    COMMANDS_BY_PATH,
    EXIT_CODES_DOC,
    ISSUE_KEY_PATTERN,
    MAX_QUERY_CHARS,
    MAX_SEARCH_LIMIT,
    PAGE_ID_PATTERN,
    SCHEMA_VERSION,
    AtlError,
    CommandSpec,
    ExitCode,
    command_schema,
    success,
)
from atlass_cli.transport import ServiceClient, sanitize_text

MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_COMMENT_BYTES = 1024 * 1024
MAX_SECRET_LINE = 4096 + 2
ISSUE_KEY_RE = re.compile(ISSUE_KEY_PATTERN)
ROOT_SUMMARY = "Agent-first JSON CLI for Jira and Confluence Data Center."
SECRET_LABELS = {"token": "personal access token", "password": "password"}
PAGE_ID_RE = re.compile(PAGE_ID_PATTERN)


@dataclass
class Runtime:
    """Process I/O and injectable dependencies (tests replace transport and keyring)."""

    stdin: BinaryIO
    stdout: TextIO
    stderr: TextIO
    cwd: Path
    stdin_isatty: Callable[[], bool]
    read_hidden: Callable[[], str]
    store_factory: Callable[[], CredentialStore] = field(default=default_store)
    transport: httpx.BaseTransport | None = None


def default_runtime() -> Runtime:
    return Runtime(
        stdin=sys.stdin.buffer,
        stdout=sys.stdout,
        stderr=sys.stderr,
        cwd=Path.cwd(),
        stdin_isatty=sys.stdin.isatty,
        read_hidden=lambda: getpass.getpass(""),
    )


# --------------------------------------------------------------------------- parser


_ARGUMENT_ERROR_RE = re.compile(r"argument ([^:]+): (.*)", re.S)
_VALUE_FREE_PREFIXES = ("the following arguments are required:", "one of the arguments ")


def _value_free_usage_message(message: str) -> str:
    """Rebuild argparse's message without any caller-supplied value (it may be a secret)."""
    if message.startswith(_VALUE_FREE_PREFIXES):
        return message
    if message.startswith("unrecognized arguments"):
        return "unrecognized arguments"
    match = _ARGUMENT_ERROR_RE.fullmatch(message)
    if not match:
        return "invalid arguments"
    detail = match.group(2)
    if detail.startswith("invalid choice"):
        detail = "invalid choice"
    elif "'" in detail or '"' in detail:
        detail = "invalid value"
    return f"argument {match.group(1)}: {detail}"


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        path = self.prog.removeprefix("atl").strip()
        hint = f"atl schema '{path}'" if path in COMMANDS_BY_PATH else "atl commands"
        raise AtlError(
            "usage_error",
            f"{self.prog}: {sanitize_text(_value_free_usage_message(message), 300)}",
            f"Run `{hint}` to see valid arguments.",
            {"usage": " ".join(self.format_usage().split())},
        )

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        raise AtlError(
            "usage_error",
            sanitize_text(message or "invalid arguments", 300),
            "Run `atl commands` to see valid commands.",
        )


def _issue_key(value: str) -> str:
    if not ISSUE_KEY_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("must be an issue key such as ABC-123")
    return value


def _page_id(value: str) -> str:
    if not PAGE_ID_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("must be a numeric page ID")
    return value


def _query(value: str) -> str:
    if not value.strip() or len(value) > MAX_QUERY_CHARS:
        raise argparse.ArgumentTypeError(f"must be 1-{MAX_QUERY_CHARS} characters")
    return value


def _status(value: str) -> str:
    if not value.strip() or len(value) > 255:
        raise argparse.ArgumentTypeError("must be a status name of 1-255 characters")
    return value


def _bounded(low: int, high: int | None) -> Callable[[str], int]:
    def parse(value: str) -> int:
        try:
            number = int(value, 10)
        except ValueError:
            raise argparse.ArgumentTypeError("must be an integer") from None
        if number < low or (high is not None and number > high):
            upper = f"-{high}" if high is not None else " or more"
            raise argparse.ArgumentTypeError(f"must be {low}{upper}")
        return number

    return parse


def build_parser() -> _Parser:
    common = _Parser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, metavar="PATH")
    root = _Parser(prog="atl", add_help=False, parents=[common])
    groups = root.add_subparsers(dest="_group", required=True, metavar="COMMAND")

    def leaf(subparsers: Any, name: str, path: str) -> _Parser:
        parser: _Parser = subparsers.add_parser(name, add_help=False, parents=[common])
        parser.set_defaults(command=path)
        return parser

    def group(subparsers: Any, name: str) -> Any:
        parser = subparsers.add_parser(name, add_help=False, parents=[common])
        return parser.add_subparsers(dest=f"_{name}", required=True, metavar="COMMAND")

    leaf(groups, "commands", "commands")
    p = leaf(groups, "schema", "schema")
    p.add_argument("path", nargs="+", metavar="COMMAND")

    p = leaf(groups, "login", "login")
    p.add_argument("--service", required=True, choices=["jira", "confluence", "all"])
    p.add_argument("--auth", default="pat", choices=["pat", "basic"])
    p.add_argument("--username")
    stdin_group = p.add_mutually_exclusive_group()
    stdin_group.add_argument("--token-stdin", action="store_true")
    stdin_group.add_argument("--password-stdin", action="store_true")

    config_group = group(groups, "config")
    p = leaf(config_group, "check", "config check")
    p.add_argument("--issue", type=_issue_key)

    jira_group = group(groups, "jira")
    story = group(jira_group, "story")
    leaf(story, "create", "jira story create").add_argument("--input", required=True)
    for name in ("requirements", "transition"):
        p = leaf(jira_group, name, f"jira {name}")
        p.add_argument("key", type=_issue_key)
        p.add_argument("--to", required=True, type=_status)
    feature = group(jira_group, "feature")
    p = leaf(feature, "find", "jira feature find")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--issue", type=_issue_key)
    mode.add_argument("--text", type=_query)
    p.add_argument("--limit", type=_bounded(1, 25), default=10)
    leaf(feature, "create", "jira feature create").add_argument("--input", required=True)
    attachment = group(jira_group, "attachment")
    p = leaf(attachment, "add", "jira attachment add")
    p.add_argument("key", type=_issue_key)
    p.add_argument("--file", required=True)
    comment = group(jira_group, "comment")
    p = leaf(comment, "add", "jira comment add")
    p.add_argument("key", type=_issue_key)
    p.add_argument("--body-file", required=True)
    p = leaf(jira_group, "search", "jira search")
    p.add_argument("--jql", required=True, type=_query)
    p.add_argument("--start", type=_bounded(0, None), default=0)
    p.add_argument("--limit", type=_bounded(1, MAX_SEARCH_LIMIT), default=50)

    confluence_group = group(groups, "confluence")
    page = group(confluence_group, "page")
    leaf(page, "read", "confluence page read").add_argument("id", type=_page_id)
    p = leaf(page, "search", "confluence page search")
    p.add_argument("--cql", required=True, type=_query)
    p.add_argument("--start", type=_bounded(0, None), default=0)
    p.add_argument("--limit", type=_bounded(1, MAX_SEARCH_LIMIT), default=50)
    leaf(page, "create", "confluence page create").add_argument("--input", required=True)
    p = leaf(page, "update", "confluence page update")
    p.add_argument("id", type=_page_id)
    p.add_argument("--input", required=True)
    return root


# --------------------------------------------------------------------------- help


def _help(argv: list[str]) -> dict[str, Any]:
    words: list[str] = []
    skip = False
    for word in argv:
        if skip:
            skip = False
            continue
        if word == "--config":
            skip = True
            continue
        if word.startswith("-"):
            continue
        words.append(word)
    prefixes = {
        " ".join(c.path.split()[:n]) for c in COMMANDS for n in range(1, len(c.path.split()) + 1)
    }
    path = ""
    for n in range(len(words), 0, -1):
        candidate = " ".join(words[:n])
        if candidate in prefixes:
            path = candidate
            break
    listed = [
        {"path": c.path, "summary": c.summary, "schema": f"atl schema '{c.path}'"}
        for c in COMMANDS
        if not path or c.path == path or c.path.startswith(path + " ")
    ]
    spec = COMMANDS_BY_PATH.get(path)
    return {
        "help": {
            "path": path or "atl",
            "summary": spec.summary if spec else ROOT_SUMMARY,
            "commands": listed,
            "note": "All output is a single JSON object on stdout. Use "
            "`atl schema '<command>'` for argument, input, success, and error schemas.",
        }
    }


# --------------------------------------------------------------------------- input helpers


def _read_source(rt: Runtime, source: str, what: str, max_bytes: int) -> str:
    if source == "-":
        data = rt.stdin.read(max_bytes + 1)
    else:
        path = Path(source)
        if not path.is_absolute():
            path = rt.cwd / path
        try:
            with path.open("rb") as handle:
                data = handle.read(max_bytes + 1)
        except OSError:
            raise AtlError(
                "input_invalid",
                f"Cannot read {what} file: {path}",
                f"Pass an existing readable file for the {what}, or '-' to read stdin.",
                {"path": str(path)},
            ) from None
    if len(data) > max_bytes:
        raise AtlError(
            "input_invalid",
            f"The {what} exceeds {max_bytes} bytes.",
            f"Reduce the size of the {what}.",
            {"max_bytes": max_bytes},
        )
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise AtlError(
            "input_invalid",
            f"The {what} is not valid UTF-8.",
            f"Save the {what} as UTF-8.",
        ) from None


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _schema_path(error: ValidationError, schema: dict[str, Any], known_keys: frozenset[str]) -> str:
    """Show schema-defined and atl.yaml-configured names, but mask other caller-defined keys."""
    parts: list[str] = []
    current: Any = schema
    for part in error.absolute_path:
        if isinstance(part, int):
            parts.append(str(part))
            current = current.get("items", {}) if isinstance(current, dict) else {}
        elif isinstance(current, dict) and part in current.get("properties", {}):
            parts.append(str(part))
            current = current["properties"][part]
        else:
            parts.append(str(part) if part in known_keys else "<field>")
            current = current.get("additionalProperties", {}) if isinstance(current, dict) else {}
    return sanitize_text("/" + "/".join(parts), 300)


def _schema_problem(
    error: ValidationError, schema: dict[str, Any], known_keys: frozenset[str]
) -> dict[str, Any]:
    """Describe a schema violation by location and rule, never by the rejected value."""
    problem: dict[str, Any] = {
        "path": _schema_path(error, schema, known_keys),
        "validator": str(error.validator),
    }
    expected = error.validator_value
    if (
        error.validator == "required"
        and isinstance(expected, list)
        and isinstance(error.instance, dict)
    ):
        problem["missing"] = [name for name in expected if name not in error.instance]
    elif error.validator == "additionalProperties" and isinstance(error.schema, dict):
        problem["allowed"] = sorted(error.schema.get("properties", {}))
    elif error.validator in ("oneOf", "anyOf"):
        problem["expected"] = "one of the alternatives in the input schema"
    elif isinstance(expected, str | int | float | bool) or (
        isinstance(expected, list) and all(isinstance(v, str | int | float) for v in expected)
    ):
        problem["expected"] = expected
    return problem


def read_json_input(
    rt: Runtime, source: str, spec: CommandSpec, known_keys: frozenset[str] = frozenset()
) -> dict[str, Any]:
    assert spec.input is not None
    text = _read_source(rt, source, "input", MAX_INPUT_BYTES)
    try:
        doc = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except ValueError as exc:
        raise AtlError(
            "input_invalid",
            f"The input is not valid JSON: {sanitize_text(str(exc), 200)}",
            f"Supply a JSON object matching `atl schema '{spec.path}'` (input).",
        ) from None
    errors = sorted(
        Draft202012Validator(spec.input).iter_errors(doc), key=lambda e: list(e.absolute_path)
    )
    if errors:
        raise AtlError(
            "input_invalid",
            f"The input does not match the {spec.path} input schema.",
            f"Correct the listed problems; see `atl schema '{spec.path}'` (input).",
            {"errors": [_schema_problem(e, spec.input, known_keys) for e in errors[:20]]},
        )
    assert isinstance(doc, dict)
    return doc


def _load(rt: Runtime, args: argparse.Namespace) -> Config:
    return load_config(getattr(args, "config", None), rt.cwd)


@contextmanager
def _client(rt: Runtime, service: str, cfg: ServiceConfig) -> Iterator[ServiceClient]:
    credential = rt.store_factory().load(service, cfg.base_url)
    client = ServiceClient(
        service, cfg.base_url, credential, ca_bundle=cfg.ca_bundle, transport=rt.transport
    )
    try:
        yield client
    finally:
        client.close()


# --------------------------------------------------------------------------- handlers


def cmd_commands(args: argparse.Namespace, rt: Runtime) -> Any:
    return {
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "commands": [
            {"path": c.path, "summary": c.summary, "schema": f"atl schema '{c.path}'"}
            for c in COMMANDS
        ],
        "exit_codes": EXIT_CODES_DOC,
    }


def cmd_schema(args: argparse.Namespace, rt: Runtime) -> Any:
    path = " ".join(" ".join(args.path).split())
    spec = COMMANDS_BY_PATH.get(path)
    if spec is None:
        raise AtlError(
            "usage_error",
            f"Unknown command {sanitize_text(path, 100)!r}.",
            "Run `atl commands` and pass one of the listed paths, e.g. "
            "`atl schema 'jira story create'`.",
            {"known": [c.path for c in COMMANDS]},
        )
    return command_schema(spec)


def _prompt(rt: Runtime, **payload: Any) -> None:
    rt.stderr.write(json.dumps({"prompt": payload}, ensure_ascii=True) + "\n")
    rt.stderr.flush()


def _read_stdin_line(rt: Runtime, label: str) -> str:
    raw = rt.stdin.readline(MAX_SECRET_LINE + 1)
    if not raw:
        raise AtlError(
            "usage_error",
            f"No {label} was provided on stdin.",
            f"Pipe the {label} on stdin, one per line.",
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise AtlError(
            "usage_error", f"The {label} is not UTF-8.", f"Supply the {label} as UTF-8."
        ) from None


def _verify_login(client: ServiceClient, service: str) -> str | None:
    if service == "jira":
        me = client.get("/rest/api/2/myself")
        user = me.get("name") if isinstance(me, dict) else None
    else:
        me = client.get("/rest/api/user/current")
        if isinstance(me, dict) and me.get("type") == "anonymous":
            me = None
        user = me.get("username") if isinstance(me, dict) else None
    if not isinstance(user, str) or not user:
        raise AtlError(
            "auth_failed",
            f"{service} did not confirm an authenticated user for this credential.",
            f"Check the credential and run `atl login --service {service}` again.",
            {"service": service},
        )
    return user


def cmd_login(args: argparse.Namespace, rt: Runtime) -> Any:
    config = _load(rt, args)
    services = ["jira", "confluence"] if args.service == "all" else [args.service]
    targets: list[tuple[str, ServiceConfig]] = [
        (s, require_jira(config) if s == "jira" else require_confluence(config)) for s in services
    ]
    if args.auth == "pat" and args.password_stdin:
        raise AtlError(
            "usage_error",
            "--password-stdin is for --auth basic.",
            "Use --token-stdin with --auth pat.",
        )
    if args.auth == "basic" and args.token_stdin:
        raise AtlError(
            "usage_error",
            "--token-stdin is for --auth pat.",
            "Use --password-stdin with --auth basic.",
        )
    if args.auth == "pat" and args.username:
        raise AtlError(
            "usage_error",
            "--username is only used with --auth basic.",
            "Remove --username or use --auth basic.",
        )
    use_stdin = bool(args.token_stdin or args.password_stdin)
    interactive = rt.stdin_isatty()
    if not use_stdin and not interactive:
        flag = "--token-stdin" if args.auth == "pat" else "--password-stdin"
        raise AtlError(
            "usage_error",
            "No terminal is available for a hidden prompt.",
            f"Pipe the secret on stdin with {flag} (one line per service).",
        )
    store = rt.store_factory()

    username: str | None = None
    if args.auth == "basic":
        if args.username:
            username = validate_username(args.username)
        elif interactive and not use_stdin:
            _prompt(rt, kind="username", message="Enter Data Center username (visible)")
            username = validate_username(_read_stdin_line(rt, "username"))
        else:
            raise AtlError("usage_error", "Basic auth needs a username.", "Pass --username NAME.")

    label = "token" if args.auth == "pat" else "password"
    results: list[dict[str, Any]] = []
    failed = False
    for service, cfg in targets:
        outcome: dict[str, Any] = {
            "service": service,
            "auth": args.auth,
            "base_url": cfg.base_url,
            "user": None,
        }
        try:
            if use_stdin:
                secret = _read_stdin_line(rt, label)
            else:
                _prompt(
                    rt,
                    kind=label,
                    service=service,
                    message=f"Enter {service} {SECRET_LABELS[label]} (input hidden)",
                )
                secret = rt.read_hidden()
            credential = Credential(
                auth=args.auth, secret=validate_secret(secret, label), username=username
            )
            client = ServiceClient(
                service, cfg.base_url, credential, ca_bundle=cfg.ca_bundle, transport=rt.transport
            )
            with client:
                outcome["user"] = _verify_login(client, service)
            store.save(service, cfg.base_url, credential)
            outcome["status"] = "stored"
        except AtlError as exc:
            if len(targets) == 1:
                raise
            failed = True
            outcome["status"] = "failed"
            outcome["error"] = exc.envelope()["error"]
        results.append(outcome)
    if failed:
        stored = [r["service"] for r in results if r["status"] == "stored"]
        failed_services = [r["service"] for r in results if r["status"] == "failed"]
        raise AtlError(
            "login_failed",
            f"Login failed for {', '.join(failed_services)}"
            + (f"; stored credentials for {', '.join(stored)}." if stored else "; nothing stored."),
            "Fix each failed service using its error.fix, then run `atl login --service <service>` "
            "for that service only.",
            {"services": results},
        )
    return {"services": results}


def cmd_config_check(args: argparse.Namespace, rt: Runtime) -> Any:
    config = _load(rt, args)
    jira_cfg = require_jira(config)
    with _client(rt, "jira", jira_cfg) as client:
        checks = jira.config_check(client, config, args.issue)
    summary: dict[str, int] = {}
    for check in checks:
        summary[check.status] = summary.get(check.status, 0) + 1
    report = [c.as_dict() for c in checks]
    if summary.get("drift"):
        raise AtlError(
            "config_drift",
            f"atl.yaml has drifted from live Jira metadata ({summary['drift']} check(s)).",
            "Apply each drift check's fix (update atl.yaml or the Jira configuration), then re-run "
            "`atl config check`.",
            {"status": "drift", "summary": summary, "checks": report},
        )
    if summary.get("unverified"):
        raise AtlError(
            "config_unverified",
            f"{summary['unverified']} check(s) could not be verified against live Jira.",
            "Apply each unverified check's fix (for transitions, pass --issue KEY for an issue in "
            "the listed status), then re-run `atl config check`.",
            {"status": "unverified", "summary": summary, "checks": report},
        )
    return {"status": "pass", "summary": summary, "checks": report}


def _jira_project_command(
    args: argparse.Namespace, rt: Runtime, spec_path: str
) -> tuple[Config, dict[str, Any]]:
    config = _load(rt, args)
    known = frozenset(
        name
        for project in (config.jira.projects.values() if config.jira else ())
        for name in project.fields
    )
    return config, read_json_input(rt, args.input, COMMANDS_BY_PATH[spec_path], known)


def cmd_story_create(args: argparse.Namespace, rt: Runtime) -> Any:
    config, doc = _jira_project_command(args, rt, "jira story create")
    jira_cfg = require_jira(config)
    project = require_project(config, doc["project"])
    with _client(rt, "jira", jira_cfg) as client:
        return jira.story_create(client, jira_cfg, project, doc)


def cmd_feature_create(args: argparse.Namespace, rt: Runtime) -> Any:
    config, doc = _jira_project_command(args, rt, "jira feature create")
    jira_cfg = require_jira(config)
    project = require_project(config, doc["project"])
    jira.require_feature_confirmation(project, doc)
    with _client(rt, "jira", jira_cfg) as client:
        return jira.feature_create(client, project, doc)


def _issue_project(config: Config, key: str) -> Any:
    return require_project(config, key.rpartition("-")[0])


def cmd_requirements(args: argparse.Namespace, rt: Runtime) -> Any:
    config = _load(rt, args)
    project = _issue_project(config, args.key)
    workflow.configured_transitions(project, args.to)
    with _client(rt, "jira", require_jira(config)) as client:
        return workflow.requirements(client, project, args.key, args.to).as_dict()


def cmd_transition(args: argparse.Namespace, rt: Runtime) -> Any:
    config = _load(rt, args)
    project = _issue_project(config, args.key)
    workflow.configured_transitions(project, args.to)
    with _client(rt, "jira", require_jira(config)) as client:
        return workflow.transition(client, project, args.key, args.to)


def cmd_feature_find(args: argparse.Namespace, rt: Runtime) -> Any:
    config = _load(rt, args)
    jira_cfg = require_jira(config)
    if args.issue is not None:
        _issue_project(config, args.issue)
    with _client(rt, "jira", jira_cfg) as client:
        return features.find(
            client, jira_cfg, issue_key=args.issue, text=args.text, limit=args.limit
        )


def cmd_attachment_add(args: argparse.Namespace, rt: Runtime) -> Any:
    config = _load(rt, args)
    jira_cfg = require_jira(config)
    path = Path(args.file)
    if not path.is_absolute():
        path = rt.cwd / path
    with _client(rt, "jira", jira_cfg) as client:
        return jira.attachment_add(client, jira_cfg, args.key, path)


def cmd_comment_add(args: argparse.Namespace, rt: Runtime) -> Any:
    config = _load(rt, args)
    jira_cfg = require_jira(config)
    body = _read_source(rt, args.body_file, "comment body", MAX_COMMENT_BYTES)
    with _client(rt, "jira", jira_cfg) as client:
        return jira.comment_add(client, args.key, body)


def cmd_jira_search(args: argparse.Namespace, rt: Runtime) -> Any:
    config = _load(rt, args)
    with _client(rt, "jira", require_jira(config)) as client:
        return jira.search(client, args.jql, args.start, args.limit)


def cmd_page_read(args: argparse.Namespace, rt: Runtime) -> Any:
    config = _load(rt, args)
    with _client(rt, "confluence", require_confluence(config)) as client:
        return confluence.read(client, args.id)


def cmd_page_search(args: argparse.Namespace, rt: Runtime) -> Any:
    config = _load(rt, args)
    with _client(rt, "confluence", require_confluence(config)) as client:
        return confluence.search(client, args.cql, args.start, args.limit)


def cmd_page_create(args: argparse.Namespace, rt: Runtime) -> Any:
    doc = read_json_input(rt, args.input, COMMANDS_BY_PATH["confluence page create"])
    config = _load(rt, args)
    with _client(rt, "confluence", require_confluence(config)) as client:
        return confluence.create(client, doc)


def cmd_page_update(args: argparse.Namespace, rt: Runtime) -> Any:
    doc = read_json_input(rt, args.input, COMMANDS_BY_PATH["confluence page update"])
    config = _load(rt, args)
    with _client(rt, "confluence", require_confluence(config)) as client:
        return confluence.update(client, args.id, doc)


HANDLERS: dict[str, Callable[[argparse.Namespace, Runtime], Any]] = {
    "commands": cmd_commands,
    "schema": cmd_schema,
    "login": cmd_login,
    "config check": cmd_config_check,
    "jira story create": cmd_story_create,
    "jira requirements": cmd_requirements,
    "jira transition": cmd_transition,
    "jira feature find": cmd_feature_find,
    "jira feature create": cmd_feature_create,
    "jira attachment add": cmd_attachment_add,
    "jira comment add": cmd_comment_add,
    "jira search": cmd_jira_search,
    "confluence page read": cmd_page_read,
    "confluence page search": cmd_page_search,
    "confluence page create": cmd_page_create,
    "confluence page update": cmd_page_update,
}


# --------------------------------------------------------------------------- main


def run(argv: list[str], rt: Runtime) -> Any:
    if any(token in ("-h", "--help") for token in argv):
        return _help(argv)
    args = build_parser().parse_args(argv)
    return HANDLERS[args.command](args, rt)


def main(argv: list[str] | None = None, runtime: Runtime | None = None) -> int:
    rt = runtime or default_runtime()
    tokens = sys.argv[1:] if argv is None else list(argv)
    exit_code: int
    try:
        envelope = success(run(tokens, rt))
        exit_code = ExitCode.OK
    except AtlError as exc:
        envelope, exit_code = exc.envelope(), exc.exit_code
    except KeyboardInterrupt:
        error = AtlError(
            "interrupted",
            "Interrupted before completion.",
            "If the command was a write, inspect the remote item before retrying.",
        )
        envelope, exit_code = error.envelope(), error.exit_code
    except Exception as exc:  # noqa: BLE001 - the JSON boundary must catch everything
        error = AtlError(
            "internal_error",
            "atl hit an unexpected internal error.",
            "Re-run the command; if it was a write, inspect the remote item first. Report the "
            "error_type to the atl maintainers if it repeats.",
            {"error_type": type(exc).__name__},
        )
        envelope, exit_code = error.envelope(), error.exit_code
    try:
        rt.stdout.write(json.dumps(envelope, ensure_ascii=True) + "\n")
        rt.stdout.flush()
    except (BrokenPipeError, OSError):
        pass
    return int(exit_code)
