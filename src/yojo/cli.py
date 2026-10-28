"""Entry point: argument parsing, dispatch, and the single output boundary.

Every invocation builds exactly one envelope and one exit code, then renders it: compact
one-line JSON when stdout is not a terminal or ``--json`` is given, otherwise the friendly
terminal view of that same envelope.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, NoReturn, TextIO, cast

import httpx
from jsonschema import Draft202012Validator, ValidationError

from yojo import __version__, bitbucket, confluence, jira, retry, workflow
from yojo.auth import (
    Credential,
    CredentialStore,
    default_store,
    validate_secret,
    validate_username,
)
from yojo.config import (
    BitbucketConfig,
    Config,
    ConfluenceConfig,
    JiraConfig,
    ServiceConfig,
    load_config,
    select_service,
)
from yojo.contracts import (
    BITBUCKET_PROJECT_PATTERN,
    COMMANDS,
    COMMANDS_BY_PATH,
    EXIT_CODES_DOC,
    GLOBAL_OPTIONS,
    ISSUE_KEY_PATTERN,
    MAX_QUERY_CHARS,
    MAX_SEARCH_LIMIT,
    PAGE_ID_PATTERN,
    PROJECT_KEY_PATTERN,
    REPO_PATTERN,
    SCHEMA_VERSION,
    CommandSpec,
    ExitCode,
    YojoError,
    command_listing,
    command_schema,
    success,
)
from yojo.render import Style, color_enabled, render
from yojo.resolve import user_lookup
from yojo.transport import ServiceClient, sanitize_text
from yojo.workflow import Assignment

MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_COMMENT_BYTES = 1024 * 1024
MAX_SECRET_LINE = 4096 + 2
MAX_FIELD_NAMES = 50
ISSUE_KEY_RE = re.compile(ISSUE_KEY_PATTERN)
PROJECT_KEY_RE = re.compile(PROJECT_KEY_PATTERN)
PAGE_ID_RE = re.compile(PAGE_ID_PATTERN)
REPO_RE = re.compile(REPO_PATTERN)
BITBUCKET_PROJECT_RE = re.compile(BITBUCKET_PROJECT_PATTERN)
MAX_DIFF_PATH = 1024
ROOT_SUMMARY = "Generic command line for Jira, Confluence, and Bitbucket Data Center."
SECRET_LABELS = {"token": "personal access token", "password": "password"}
VALUE_OPTIONS = ("--config", "--base-url")


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
    stdout_isatty: Callable[[], bool] = field(default=lambda: False)
    environ: Mapping[str, str] = field(default_factory=dict)
    json_mode: bool = True


def default_runtime() -> Runtime:
    return Runtime(
        stdin=sys.stdin.buffer,
        stdout=sys.stdout,
        stderr=sys.stderr,
        cwd=Path.cwd(),
        stdin_isatty=sys.stdin.isatty,
        read_hidden=lambda: getpass.getpass(""),
        stdout_isatty=sys.stdout.isatty,
        environ=os.environ,
    )


# --------------------------------------------------------------------------- parser


_ARGUMENT_ERROR_RE = re.compile(r"argument ([^:]+): (.*)", re.S)
_VALUE_FREE_PREFIXES = ("the following arguments are required:", "one of the arguments ")


def _value_free_usage_message(message: str) -> str:
    """Rebuild argparse's message without any caller-supplied value (it may be sensitive)."""
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
        path = self.prog.removeprefix("yojo").strip()
        spec = COMMANDS_BY_PATH.get(path)
        hint = spec.usage if spec else "yojo commands"
        raise YojoError(
            "usage_error",
            f"{self.prog}: {sanitize_text(_value_free_usage_message(message), 300)}",
            f"Use `{hint}`"
            + (f", for example `{spec.example}`." if spec else " to list commands."),
            {"usage": spec.usage if spec else " ".join(self.format_usage().split())},
        )

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        raise YojoError(
            "usage_error",
            sanitize_text(message or "invalid arguments", 300),
            "Run `yojo commands` to see valid commands.",
        )


def _issue_key(value: str) -> str:
    if not ISSUE_KEY_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("must be an issue key such as PROJ-123")
    return value


def _project_key(value: str) -> str:
    if not PROJECT_KEY_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("must be an uppercase project key such as PROJ")
    return value


def _page_id(value: str) -> str:
    if not PAGE_ID_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("must be a numeric page ID")
    return value


def _repo(value: str) -> str:
    if not REPO_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("must be a repository as PROJECT/slug, such as PROJ/web")
    return value


def _bitbucket_project(value: str) -> str:
    if not BITBUCKET_PROJECT_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("must be a Bitbucket project key such as PROJ")
    return value


def _diff_path(value: str) -> str:
    parts = value.split("/")
    if (
        len(value) > MAX_DIFF_PATH
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise argparse.ArgumentTypeError("must be a repository-relative file path such as src/a.py")
    return value


def _pr_state(value: str) -> str:
    return value.upper()


def _query(value: str) -> str:
    if not value.strip() or len(value) > MAX_QUERY_CHARS:
        raise argparse.ArgumentTypeError(f"must be 1-{MAX_QUERY_CHARS} characters")
    return value


def _name(value: str) -> str:
    if not value.strip() or len(value) > 255:
        raise argparse.ArgumentTypeError("must be a name of 1-255 characters")
    return value


def _set_item(value: str) -> str:
    name, sep, _ = value.partition("=")
    if not sep or not name.strip():
        raise argparse.ArgumentTypeError("must be NAME=VALUE, for example 'Story Points=3'")
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
    common.add_argument("--base-url", default=argparse.SUPPRESS, metavar="URL")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    root = _Parser(prog="yojo", add_help=False, parents=[common])
    groups = root.add_subparsers(dest="_group", required=True, metavar="COMMAND")

    def leaf(subparsers: Any, name: str, path: str) -> _Parser:
        parser: _Parser = subparsers.add_parser(name, add_help=False, parents=[common])
        parser.set_defaults(command=path)
        return parser

    def group(subparsers: Any, name: str) -> Any:
        parser = subparsers.add_parser(name, add_help=False, parents=[common])
        return parser.add_subparsers(dest=f"_{name}", required=True, metavar="COMMAND")

    def sets(parser: _Parser) -> None:
        parser.add_argument("--set", action="append", type=_set_item, metavar="NAME=VALUE")

    leaf(groups, "commands", "commands")
    p = leaf(groups, "schema", "schema")
    p.add_argument("path", nargs="+", metavar="COMMAND")

    p = leaf(groups, "login", "login")
    p.add_argument("--service", required=True, choices=["jira", "confluence", "bitbucket", "all"])
    p.add_argument("--auth", default="pat", choices=["pat", "basic"])
    p.add_argument("--username")
    stdin_group = p.add_mutually_exclusive_group()
    stdin_group.add_argument("--token-stdin", action="store_true")
    stdin_group.add_argument("--password-stdin", action="store_true")

    config_group = group(groups, "config")
    leaf(config_group, "check", "config check")

    jira_group = group(groups, "jira")
    issue = group(jira_group, "issue")
    p = leaf(issue, "create", "jira issue create")
    p.add_argument("project", nargs="?", type=_project_key, metavar="PROJECT")
    p.add_argument("--type", type=_name, metavar="TYPE")
    p.add_argument("--summary", metavar="TEXT")
    p.add_argument("--description", metavar="TEXT")
    sets(p)
    p.add_argument("--input", metavar="FILE")
    p = leaf(issue, "get", "jira issue get")
    p.add_argument("key", type=_issue_key)
    p.add_argument("--fields", metavar="NAMES")
    p.add_argument("--full", action="store_true")
    p = leaf(issue, "update", "jira issue update")
    p.add_argument("key", type=_issue_key)
    p.add_argument("--summary", metavar="TEXT")
    p.add_argument("--description", metavar="TEXT")
    sets(p)
    p.add_argument("--input", metavar="FILE")
    p = leaf(issue, "link", "jira issue link")
    p.add_argument("key", type=_issue_key)
    p.add_argument("other", type=_issue_key, metavar="OTHER_KEY")
    p.add_argument("--type", required=True, type=_name, metavar="LINK_TYPE")

    p = leaf(jira_group, "requirements", "jira requirements")
    p.add_argument("key", type=_issue_key)
    p.add_argument("--to", required=True, type=_name, metavar="STATUS")
    sets(p)
    p = leaf(jira_group, "transition", "jira transition")
    p.add_argument("key", type=_issue_key)
    p.add_argument("--to", required=True, type=_name, metavar="STATUS")
    sets(p)
    p.add_argument("--input", metavar="FILE")

    attachment = group(jira_group, "attachment")
    p = leaf(attachment, "add", "jira attachment add")
    p.add_argument("key", type=_issue_key)
    p.add_argument("--file", required=True)
    comment = group(jira_group, "comment")
    p = leaf(comment, "add", "jira comment add")
    p.add_argument("key", type=_issue_key)
    body = p.add_mutually_exclusive_group(required=True)
    body.add_argument("--body", metavar="TEXT")
    body.add_argument("--body-file", metavar="FILE")
    p = leaf(jira_group, "search", "jira search")
    p.add_argument("--jql", required=True, type=_query)
    p.add_argument("--start", type=_bounded(0, None), default=0)
    p.add_argument("--limit", type=_bounded(1, MAX_SEARCH_LIMIT), default=20)

    confluence_group = group(groups, "confluence")
    page = group(confluence_group, "page")
    p = leaf(page, "read", "confluence page read")
    p.add_argument("id", type=_page_id)
    p.add_argument("--full", action="store_true")
    p = leaf(page, "search", "confluence page search")
    p.add_argument("--cql", required=True, type=_query)
    p.add_argument("--start", type=_bounded(0, None), default=0)
    p.add_argument("--limit", type=_bounded(1, MAX_SEARCH_LIMIT), default=20)
    leaf(page, "create", "confluence page create").add_argument("--input", required=True)
    p = leaf(page, "update", "confluence page update")
    p.add_argument("id", type=_page_id)
    p.add_argument("--input", required=True)

    def paging(parser: _Parser) -> None:
        parser.add_argument("--start", type=_bounded(0, None), default=0)
        parser.add_argument("--limit", type=_bounded(1, MAX_SEARCH_LIMIT), default=20)

    def repo_flag(parser: _Parser, required: bool = True) -> None:
        parser.add_argument("--repo", required=required, type=_repo, metavar="PROJECT/slug")

    bitbucket_group = group(groups, "bitbucket")
    repo = group(bitbucket_group, "repo")
    p = leaf(repo, "list", "bitbucket repo list")
    p.add_argument("--project", type=_bitbucket_project, metavar="PROJECT")
    paging(p)
    p = leaf(repo, "get", "bitbucket repo get")
    p.add_argument("repo", type=_repo, metavar="PROJECT/slug")
    branch = group(bitbucket_group, "branch")
    p = leaf(branch, "list", "bitbucket branch list")
    repo_flag(p)
    p.add_argument("--filter", type=_name, metavar="TEXT")
    paging(p)
    pr = group(bitbucket_group, "pr")
    p = leaf(pr, "list", "bitbucket pr list")
    repo_flag(p)
    p.add_argument(
        "--state",
        type=_pr_state,
        choices=["OPEN", "MERGED", "DECLINED", "ALL"],
        default="OPEN",
        metavar="OPEN|MERGED|DECLINED|ALL",
    )
    paging(p)
    p = leaf(pr, "get", "bitbucket pr get")
    p.add_argument("id", type=_bounded(1, None), metavar="ID")
    repo_flag(p)
    p.add_argument("--full", action="store_true")
    p = leaf(pr, "diff", "bitbucket pr diff")
    p.add_argument("id", type=_bounded(1, None), metavar="ID")
    repo_flag(p)
    p.add_argument("--path", type=_diff_path, metavar="FILE")
    p.add_argument("--old-path", type=_diff_path, metavar="FILE")
    p.add_argument("--full", action="store_true")
    p = leaf(pr, "create", "bitbucket pr create")
    repo_flag(p, required=False)
    p.add_argument("--source", metavar="BRANCH")
    p.add_argument("--target", metavar="BRANCH")
    p.add_argument("--title", metavar="TEXT")
    p.add_argument("--description", metavar="TEXT")
    p.add_argument("--input", metavar="FILE")
    p = leaf(pr, "comment", "bitbucket pr comment")
    p.add_argument("id", type=_bounded(1, None), metavar="ID")
    repo_flag(p)
    body = p.add_mutually_exclusive_group(required=True)
    body.add_argument("--body", metavar="TEXT")
    body.add_argument("--body-file", metavar="FILE")
    return root


# --------------------------------------------------------------------------- help


def _words(argv: list[str]) -> list[str]:
    words: list[str] = []
    skip = False
    for word in argv:
        if skip:
            skip = False
            continue
        if word in VALUE_OPTIONS:
            skip = True
            continue
        if word.startswith("-"):
            continue
        words.append(word)
    return words


def _help(argv: list[str]) -> dict[str, Any]:
    words = _words(argv)
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
        command_listing(c)
        for c in COMMANDS
        if not path or c.path == path or c.path.startswith(path + " ")
    ]
    spec = COMMANDS_BY_PATH.get(path)
    return {
        "help": {
            "path": path or "yojo",
            "summary": spec.summary if spec else ROOT_SUMMARY,
            "commands": listed,
            "global_options": GLOBAL_OPTIONS,
            "note": "In a terminal yojo shows a readable view; piped or with --json it prints "
            "one JSON object. `yojo schema '<command>'` gives the full contract.",
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
            raise YojoError(
                "input_invalid",
                f"Cannot read {what} file: {path}",
                f"Pass an existing readable file for the {what}, or '-' to read stdin.",
                {"path": str(path)},
            ) from None
    if len(data) > max_bytes:
        raise YojoError(
            "input_invalid",
            f"The {what} exceeds {max_bytes} bytes.",
            f"Reduce the size of the {what}.",
            {"max_bytes": max_bytes},
        )
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise YojoError(
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


def _schema_path(error: ValidationError, schema: dict[str, Any]) -> str:
    """Show schema-defined names, but mask caller-defined keys such as field names."""
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
            parts.append("<field>")
            current = current.get("additionalProperties", {}) if isinstance(current, dict) else {}
    return sanitize_text("/" + "/".join(parts), 300)


def _schema_problem(error: ValidationError, schema: dict[str, Any]) -> dict[str, Any]:
    """Describe a schema violation by location and rule, never by the rejected value."""
    problem: dict[str, Any] = {
        "path": _schema_path(error, schema),
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


def read_json_input(rt: Runtime, source: str, spec: CommandSpec) -> dict[str, Any]:
    assert spec.input is not None
    text = _read_source(rt, source, "input", MAX_INPUT_BYTES)
    try:
        doc = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except ValueError as exc:
        raise YojoError(
            "input_invalid",
            f"The input is not valid JSON: {sanitize_text(str(exc), 200)}",
            f"Supply a JSON object matching `yojo schema '{spec.path}'` (input).",
        ) from None
    errors = sorted(
        Draft202012Validator(spec.input).iter_errors(doc), key=lambda e: list(e.absolute_path)
    )
    if errors:
        raise YojoError(
            "input_invalid",
            f"The input does not match the {spec.path} input schema.",
            f"Correct the listed problems; see `yojo schema '{spec.path}'` (input).",
            {"errors": [_schema_problem(e, spec.input) for e in errors[:20]]},
        )
    assert isinstance(doc, dict)
    return doc


def _input_doc(rt: Runtime, args: argparse.Namespace, path: str) -> dict[str, Any]:
    source = getattr(args, "input", None)
    return read_json_input(rt, source, COMMANDS_BY_PATH[path]) if source is not None else {}


def _merged(args: argparse.Namespace, doc: dict[str, Any], name: str) -> Any:
    """One value from a flag or the input; both with different values is refused."""
    flag = getattr(args, name, None)
    if flag is not None and name in doc and doc[name] != flag:
        raise YojoError(
            "input_invalid",
            f"--{name} and the input's {name!r} disagree; nothing was sent.",
            f"Give {name} once, either as --{name} or in the input.",
            {"conflicts": [name]},
        )
    return flag if flag is not None else doc.get(name)


def _assignments(args: argparse.Namespace, doc: dict[str, Any]) -> list[Assignment]:
    """Collect --set items and input fields; a repeated field with another value is refused."""
    found: dict[str, Assignment] = {}
    conflicts: list[str] = []
    items: list[tuple[str, Any, str]] = []
    for item in getattr(args, "set", None) or []:
        name, _, value = item.partition("=")
        items.append((name.strip(), value, "flag"))
    for name, value in (doc.get("fields") or {}).items():
        items.append((name.strip(), value, "input"))
    for name, value, source in items:
        key = name.casefold()
        if key in found:
            if found[key].value != value and name not in conflicts:
                conflicts.append(name)
            continue
        found[key] = Assignment(name, value, source)
    if conflicts:
        raise YojoError(
            "input_invalid",
            f"{', '.join(repr(c) for c in conflicts)} given more than once with different values; "
            "nothing was sent.",
            "Give each field once (--set or input fields, not both with different values).",
            {"conflicts": conflicts},
        )
    return list(found.values())


def _load(rt: Runtime, args: argparse.Namespace) -> Config | None:
    return load_config(getattr(args, "config", None), rt.cwd)


def _service(rt: Runtime, args: argparse.Namespace, service: str) -> ServiceConfig:
    return select_service(_load(rt, args), service, getattr(args, "base_url", None))


def _jira(rt: Runtime, args: argparse.Namespace) -> JiraConfig:
    return cast(JiraConfig, _service(rt, args, "jira"))


def _progress(rt: Runtime, message: str) -> None:
    if rt.json_mode:
        return
    style = Style(color_enabled(rt.environ))
    try:
        rt.stderr.write(style.dim("Sending: ") + sanitize_text(message, 300) + "\n")
        rt.stderr.flush()
    except OSError:
        pass


@contextmanager
def _client(rt: Runtime, service: str, cfg: ServiceConfig) -> Iterator[ServiceClient]:
    credential = rt.store_factory().load(service, cfg.base_url)
    client = ServiceClient(
        service, cfg.base_url, credential, ca_bundle=cfg.ca_bundle, transport=rt.transport
    )
    client.announce = lambda message: _progress(rt, message)
    try:
        yield client
    finally:
        client.close()


# --------------------------------------------------------------------------- handlers


def cmd_commands(args: argparse.Namespace, rt: Runtime) -> Any:
    return {
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "commands": [command_listing(c) for c in COMMANDS],
        "global_options": GLOBAL_OPTIONS,
        "exit_codes": EXIT_CODES_DOC,
    }


def cmd_schema(args: argparse.Namespace, rt: Runtime) -> Any:
    path = " ".join(" ".join(args.path).split())
    spec = COMMANDS_BY_PATH.get(path)
    if spec is None:
        raise YojoError(
            "usage_error",
            f"Unknown command {sanitize_text(path, 100)!r}.",
            "Run `yojo commands` and pass one of the listed paths, e.g. "
            "`yojo schema 'jira issue create'`.",
            {"known": [c.path for c in COMMANDS]},
        )
    return command_schema(spec)


def _prompt(rt: Runtime, message: str, **payload: Any) -> None:
    if rt.json_mode:
        rt.stderr.write(json.dumps({"prompt": {**payload, "message": message}}) + "\n")
    else:
        rt.stderr.write(message + ": ")
    rt.stderr.flush()


def _read_stdin_line(rt: Runtime, label: str) -> str:
    raw = rt.stdin.readline(MAX_SECRET_LINE + 1)
    if not raw:
        raise YojoError(
            "usage_error",
            f"No {label} was provided on stdin.",
            f"Pipe the {label} on stdin, one per line.",
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise YojoError(
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
        raise YojoError(
            "auth_failed",
            f"{service} did not confirm an authenticated user for this credential.",
            f"Check the credential and run `yojo login --service {service}` again.",
            {"service": service},
        )
    return user


def cmd_login(args: argparse.Namespace, rt: Runtime) -> Any:
    if args.service == "bitbucket" and (args.auth == "basic" or args.password_stdin):
        raise YojoError(
            "usage_error",
            "Bitbucket login takes a personal access token, not a password.",
            "Create an HTTP access token in Bitbucket (Profile > Manage account > HTTP access "
            "tokens), then run `yojo login --service bitbucket --base-url URL` and paste it, or "
            "pipe it with --token-stdin.",
        )
    if args.service == "all" and getattr(args, "base_url", None) is not None:
        raise YojoError(
            "usage_error",
            "--base-url names one instance, but --service all logs in to two.",
            "Run `yojo login --service jira --base-url URL` and "
            "`yojo login --service confluence --base-url URL` separately.",
        )
    services = ["jira", "confluence"] if args.service == "all" else [args.service]
    targets = [(s, _service(rt, args, s)) for s in services]
    if args.auth == "pat" and args.password_stdin:
        raise YojoError(
            "usage_error",
            "--password-stdin is for --auth basic.",
            "Use --token-stdin with --auth pat.",
        )
    if args.auth == "basic" and args.token_stdin:
        raise YojoError(
            "usage_error",
            "--token-stdin is for --auth pat.",
            "Use --password-stdin with --auth basic.",
        )
    if args.auth == "pat" and args.username:
        raise YojoError(
            "usage_error",
            "--username is only used with --auth basic.",
            "Remove --username or use --auth basic.",
        )
    use_stdin = bool(args.token_stdin or args.password_stdin)
    interactive = rt.stdin_isatty()
    if not use_stdin and not interactive:
        flag = "--token-stdin" if args.auth == "pat" else "--password-stdin"
        raise YojoError(
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
            _prompt(rt, "Data Center username (visible)", kind="username")
            username = validate_username(_read_stdin_line(rt, "username"))
        else:
            raise YojoError("usage_error", "Basic auth needs a username.", "Pass --username NAME.")

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
                    f"{service} {SECRET_LABELS[label]} for {cfg.base_url} (input hidden)",
                    kind=label,
                    service=service,
                )
                secret = rt.read_hidden()
            credential = Credential(
                auth=args.auth, secret=validate_secret(secret, label), username=username
            )
            client = ServiceClient(
                service, cfg.base_url, credential, ca_bundle=cfg.ca_bundle, transport=rt.transport
            )
            with client:
                if service == "bitbucket":
                    outcome["version"] = bitbucket.verify_login(client)
                else:
                    outcome["user"] = _verify_login(client, service)
            store.save(service, cfg.base_url, credential)
            outcome["status"] = "stored"
        except YojoError as exc:
            if len(targets) == 1:
                raise
            failed = True
            outcome["status"] = "failed"
            outcome["error"] = exc.envelope()["error"]
        results.append(outcome)
    if failed:
        stored = [r["service"] for r in results if r["status"] == "stored"]
        failed_services = [r["service"] for r in results if r["status"] == "failed"]
        raise YojoError(
            "login_failed",
            f"Login failed for {', '.join(failed_services)}"
            + (f"; stored credentials for {', '.join(stored)}." if stored else "; nothing stored."),
            "Fix each failed service using its error.fix, then run "
            "`yojo login --service <service>` for that service only.",
            {"services": results},
        )
    return {"services": results}


def cmd_config_check(args: argparse.Namespace, rt: Runtime) -> Any:
    config = _load(rt, args)
    if config is None:
        raise YojoError(
            "config_not_found",
            "No yojo.yaml was found, so there is nothing to check.",
            "yojo works without one. To add team aliases and rules, create yojo.yaml from "
            "yojo.example.yaml in the repository root, or pass --config PATH.",
            {"searched_from": str(rt.cwd)},
        )
    base_url = getattr(args, "base_url", None)
    checks: list[workflow.Check] = []
    services: list[str] = []
    # --base-url names one instance: Jira's when Jira is configured, otherwise Bitbucket's.
    if config.jira is not None or config.bitbucket is None:
        jira_cfg = cast(JiraConfig, select_service(config, "jira", base_url))
        with _client(rt, "jira", jira_cfg) as client:
            checks += jira.config_check(client, config, jira_cfg)
        services.append("Jira")
        base_url = None
    if config.bitbucket is not None:
        bitbucket_cfg = select_service(config, "bitbucket", base_url)
        checks += _bitbucket_config_checks(rt, config, bitbucket_cfg)
        services.append("Bitbucket")
    live = " and ".join(services)
    summary: dict[str, int] = {}
    for check in checks:
        summary[check.status] = summary.get(check.status, 0) + 1
    report = [c.as_dict() for c in checks]
    if summary.get("drift"):
        raise YojoError(
            "config_drift",
            f"yojo.yaml no longer matches live {live} ({summary['drift']} check(s)).",
            "Apply each drift check's fix (update yojo.yaml or the Jira configuration), then "
            "re-run `yojo config check`.",
            {"status": "drift", "summary": summary, "checks": report},
        )
    if summary.get("unverified"):
        raise YojoError(
            "config_unverified",
            f"{summary['unverified']} check(s) could not be verified against live {live}.",
            "Apply each unverified check's fix, then re-run `yojo config check`.",
            {"status": "unverified", "summary": summary, "checks": report},
        )
    return {"status": "pass", "summary": summary, "checks": report}


def _bitbucket_config_checks(
    rt: Runtime, config: Config, cfg: ServiceConfig
) -> list[workflow.Check]:
    try:
        with _client(rt, "bitbucket", cfg) as client:
            return bitbucket.config_check(client, str(config.path))
    except YojoError as exc:
        return [workflow.Check("bitbucket:auth", "unverified", exc.message, exc.fix)]


def cmd_issue_create(args: argparse.Namespace, rt: Runtime) -> Any:
    doc = _input_doc(rt, args, "jira issue create")
    project = _merged(args, doc, "project")
    request = jira.IssueRequest(
        project=project,
        type=_merged(args, doc, "type"),
        summary=_merged(args, doc, "summary"),
        description=_merged(args, doc, "description"),
        assignments=_assignments(args, doc),
    )
    if project is None:
        raise YojoError(
            "usage_error",
            "The project key is missing; nothing was sent.",
            "Pass it first, e.g. `yojo jira issue create PROJ --type Task --summary '...'`.",
        )
    jira_cfg = _jira(rt, args)
    with _client(rt, "jira", jira_cfg) as client:
        return jira.issue_create(client, jira_cfg, request, user_lookup(client))


def _field_names(raw: str | None) -> list[str]:
    names = [n.strip() for n in (raw or "").split(",") if n.strip()]
    if len(names) > MAX_FIELD_NAMES:
        raise YojoError(
            "usage_error",
            f"--fields lists more than {MAX_FIELD_NAMES} names.",
            "Ask for fewer fields per call.",
        )
    return names


def cmd_issue_get(args: argparse.Namespace, rt: Runtime) -> Any:
    names = _field_names(args.fields)
    jira_cfg = _jira(rt, args)
    with _client(rt, "jira", jira_cfg) as client:
        return jira.issue_get(client, jira_cfg, args.key, names, args.full)


def cmd_issue_update(args: argparse.Namespace, rt: Runtime) -> Any:
    doc = _input_doc(rt, args, "jira issue update")
    assignments = [
        Assignment(name, value, "flag")
        for name in ("summary", "description")
        if (value := _merged(args, doc, name)) is not None
    ]
    assignments += _assignments(args, doc)
    if not assignments:
        raise YojoError(
            "input_invalid",
            "Nothing to update; nothing was sent.",
            "Pass at least one change, e.g. "
            f"{retry.posix(['yojo', 'jira', 'issue', 'update', args.key, '--set', 'Labels=ui'])}.",
        )
    jira_cfg = _jira(rt, args)
    with _client(rt, "jira", jira_cfg) as client:
        return jira.issue_update(client, jira_cfg, args.key, assignments, user_lookup(client))


def cmd_issue_link(args: argparse.Namespace, rt: Runtime) -> Any:
    jira_cfg = _jira(rt, args)
    with _client(rt, "jira", jira_cfg) as client:
        return jira.issue_link(client, args.key, args.other, args.type)


def cmd_requirements(args: argparse.Namespace, rt: Runtime) -> Any:
    assignments = _assignments(args, {})
    jira_cfg = _jira(rt, args)
    with _client(rt, "jira", jira_cfg) as client:
        checklist = workflow.requirements(
            client, jira_cfg, args.key, args.to, assignments, user_lookup(client)
        )
        return checklist.as_dict()


def cmd_transition(args: argparse.Namespace, rt: Runtime) -> Any:
    doc = _input_doc(rt, args, "jira transition")
    assignments = _assignments(args, doc)
    jira_cfg = _jira(rt, args)
    with _client(rt, "jira", jira_cfg) as client:
        return workflow.transition(
            client,
            jira_cfg,
            args.key,
            args.to,
            assignments,
            user_lookup(client),
            list(args.set or []),
        )


def cmd_attachment_add(args: argparse.Namespace, rt: Runtime) -> Any:
    jira_cfg = _jira(rt, args)
    path = Path(args.file)
    if not path.is_absolute():
        path = rt.cwd / path
    with _client(rt, "jira", jira_cfg) as client:
        return jira.attachment_add(client, jira_cfg, args.key, path)


def cmd_comment_add(args: argparse.Namespace, rt: Runtime) -> Any:
    body = (
        args.body
        if args.body is not None
        else _read_source(rt, args.body_file, "comment body", MAX_COMMENT_BYTES)
    )
    if len(body.encode("utf-8")) > MAX_COMMENT_BYTES:
        raise YojoError(
            "input_invalid",
            f"The comment body exceeds {MAX_COMMENT_BYTES} bytes.",
            "Shorten the comment.",
            {"max_bytes": MAX_COMMENT_BYTES},
        )
    jira_cfg = _jira(rt, args)
    with _client(rt, "jira", jira_cfg) as client:
        return jira.comment_add(client, args.key, body)


def cmd_jira_search(args: argparse.Namespace, rt: Runtime) -> Any:
    jira_cfg = _jira(rt, args)
    with _client(rt, "jira", jira_cfg) as client:
        return jira.search(client, args.jql, args.start, args.limit)


def _confluence(rt: Runtime, args: argparse.Namespace) -> ConfluenceConfig:
    return cast(ConfluenceConfig, _service(rt, args, "confluence"))


def cmd_page_read(args: argparse.Namespace, rt: Runtime) -> Any:
    with _client(rt, "confluence", _confluence(rt, args)) as client:
        return confluence.read(client, args.id, args.full)


def cmd_page_search(args: argparse.Namespace, rt: Runtime) -> Any:
    with _client(rt, "confluence", _confluence(rt, args)) as client:
        return confluence.search(client, args.cql, args.start, args.limit)


def cmd_page_create(args: argparse.Namespace, rt: Runtime) -> Any:
    doc = read_json_input(rt, args.input, COMMANDS_BY_PATH["confluence page create"])
    with _client(rt, "confluence", _confluence(rt, args)) as client:
        return confluence.create(client, doc)


def cmd_page_update(args: argparse.Namespace, rt: Runtime) -> Any:
    doc = read_json_input(rt, args.input, COMMANDS_BY_PATH["confluence page update"])
    with _client(rt, "confluence", _confluence(rt, args)) as client:
        return confluence.update(client, args.id, doc)


def _bitbucket(rt: Runtime, args: argparse.Namespace) -> BitbucketConfig:
    return cast(BitbucketConfig, _service(rt, args, "bitbucket"))


def cmd_repo_list(args: argparse.Namespace, rt: Runtime) -> Any:
    with _client(rt, "bitbucket", _bitbucket(rt, args)) as client:
        return bitbucket.repo_list(client, args.project, args.start, args.limit)


def cmd_repo_get(args: argparse.Namespace, rt: Runtime) -> Any:
    with _client(rt, "bitbucket", _bitbucket(rt, args)) as client:
        return bitbucket.repo_get(client, args.repo)


def cmd_branch_list(args: argparse.Namespace, rt: Runtime) -> Any:
    with _client(rt, "bitbucket", _bitbucket(rt, args)) as client:
        return bitbucket.branch_list(client, args.repo, args.filter, args.start, args.limit)


def cmd_pr_list(args: argparse.Namespace, rt: Runtime) -> Any:
    with _client(rt, "bitbucket", _bitbucket(rt, args)) as client:
        return bitbucket.pr_list(client, args.repo, args.state, args.start, args.limit)


def cmd_pr_get(args: argparse.Namespace, rt: Runtime) -> Any:
    with _client(rt, "bitbucket", _bitbucket(rt, args)) as client:
        return bitbucket.pr_get(client, args.repo, args.id, args.full)


def cmd_pr_diff(args: argparse.Namespace, rt: Runtime) -> Any:
    with _client(rt, "bitbucket", _bitbucket(rt, args)) as client:
        return bitbucket.pr_diff(
            client, args.repo, args.id, args.path, args.full, args.old_path
        )


def cmd_pr_create(args: argparse.Namespace, rt: Runtime) -> Any:
    doc = _input_doc(rt, args, "bitbucket pr create")
    names = ("repo", "source", "target", "title", "description")
    values = {name: _merged(args, doc, name) for name in names}
    origins = {
        name: "flag"
        if getattr(args, name, None) is not None
        else "input"
        if name in doc
        else "missing"
        for name in names
    }
    request = bitbucket.PullRequestRequest(**values, origins=origins)
    if request.repo is None:
        raise bitbucket.pull_request_refusal(request)
    with _client(rt, "bitbucket", _bitbucket(rt, args)) as client:
        return bitbucket.pr_create(client, request)


def cmd_pr_comment(args: argparse.Namespace, rt: Runtime) -> Any:
    body = (
        args.body
        if args.body is not None
        else _read_source(rt, args.body_file, "comment body", MAX_COMMENT_BYTES)
    )
    if len(body.encode("utf-8")) > MAX_COMMENT_BYTES:
        raise YojoError(
            "input_invalid",
            f"The comment body exceeds {MAX_COMMENT_BYTES} bytes.",
            "Shorten the comment.",
            {"max_bytes": MAX_COMMENT_BYTES},
        )
    origin = "flag" if args.body is not None else "file"
    with _client(rt, "bitbucket", _bitbucket(rt, args)) as client:
        return bitbucket.pr_comment(client, args.repo, args.id, body, origin)


HANDLERS: dict[str, Callable[[argparse.Namespace, Runtime], Any]] = {
    "commands": cmd_commands,
    "schema": cmd_schema,
    "login": cmd_login,
    "config check": cmd_config_check,
    "jira issue create": cmd_issue_create,
    "jira issue get": cmd_issue_get,
    "jira issue update": cmd_issue_update,
    "jira issue link": cmd_issue_link,
    "jira requirements": cmd_requirements,
    "jira transition": cmd_transition,
    "jira attachment add": cmd_attachment_add,
    "jira comment add": cmd_comment_add,
    "jira search": cmd_jira_search,
    "confluence page read": cmd_page_read,
    "confluence page search": cmd_page_search,
    "confluence page create": cmd_page_create,
    "confluence page update": cmd_page_update,
    "bitbucket repo list": cmd_repo_list,
    "bitbucket repo get": cmd_repo_get,
    "bitbucket branch list": cmd_branch_list,
    "bitbucket pr list": cmd_pr_list,
    "bitbucket pr get": cmd_pr_get,
    "bitbucket pr diff": cmd_pr_diff,
    "bitbucket pr create": cmd_pr_create,
    "bitbucket pr comment": cmd_pr_comment,
}


# --------------------------------------------------------------------------- main


def _no_command() -> YojoError:
    return YojoError(
        "usage_error",
        "No command given.",
        "Run `yojo commands` to list commands, for example "
        "`yojo jira issue get PROJ-123 --base-url https://jira.example.com`.",
        {"usage": "yojo [--json] [--base-url URL] [--config PATH] COMMAND ..."},
    )


def run(argv: list[str], rt: Runtime) -> Any:
    if any(token in ("-h", "--help") for token in argv):
        return _help(argv)
    if not _words(argv):
        raise _no_command()
    args = build_parser().parse_args(argv)
    data = HANDLERS[args.command](args, rt)
    _check_bitbucket_read_size(args, argv, data)
    return data


def _check_bitbucket_read_size(args: argparse.Namespace, argv: list[str], data: Any) -> None:
    """Keep ordinary Bitbucket read envelopes within the documented 4 KiB budget."""
    command = args.command
    if command not in {
        "bitbucket repo list",
        "bitbucket repo get",
        "bitbucket branch list",
        "bitbucket pr list",
        "bitbucket pr get",
    } or getattr(args, "full", False):
        return
    size = len(json.dumps(success(data), ensure_ascii=True, separators=(",", ":")).encode()) + 1
    if size <= 4 * 1024:
        return
    if command.endswith(" list"):
        if args.limit == 1:
            fix = "Open the list in Bitbucket; one returned item exceeds compact output limits."
        else:
            smaller = max(1, args.limit // 2)
            corrected = retry.apply(argv, [retry.Edit("flag", "--limit", str(smaller))])
            assert corrected is not None
            fix = f"Run `{retry.posix(corrected)}` to fetch a smaller page."
    elif command == "bitbucket pr get":
        corrected = [retry.PROGRAM, *argv, "--full"]
        fix = f"Run `{retry.posix(corrected)}` to request the full pull request."
    else:
        fix = f"Open the repository in Bitbucket: {data['url']}"
    raise YojoError(
        "remote_limit_exceeded",
        "The Bitbucket read result exceeds the 4 KiB compact output limit.",
        fix,
        {"max_bytes": 4 * 1024, "result_bytes": size},
    )


def command_path(argv: list[str]) -> str | None:
    words = _words(argv)
    for n in range(len(words), 0, -1):
        if " ".join(words[:n]) in COMMANDS_BY_PATH:
            return " ".join(words[:n])
    return None


def _attach_retry(error: YojoError, argv: list[str]) -> None:
    """A refused write sent nothing, so the corrected full command is safe to offer."""
    edits = [c.retry for c in workflow.failed(error.checks) if c.retry is not None]
    corrected = retry.apply(argv, edits)
    error.details["retry"] = retry.posix(corrected) if corrected else None
    error.details["retry_argv"] = corrected
    if corrected and "-" in corrected and "--input" in corrected:
        error.details["retry_note"] = "Pipe the same --input content on stdin again."


def outcome(tokens: list[str], rt: Runtime) -> tuple[dict[str, Any], int]:
    """Build the one envelope and exit code for ``tokens``; never raises."""
    try:
        return success(run(tokens, rt)), int(ExitCode.OK)
    except YojoError as exc:
        if exc.code == "requirements_unmet" and exc.checks:
            _attach_retry(exc, tokens)
        return exc.envelope(), int(exc.exit_code)
    except KeyboardInterrupt:
        error = YojoError(
            "interrupted",
            "Interrupted before completion.",
            "If the command was a write, inspect the remote item before running it again.",
        )
    except Exception as exc:  # noqa: BLE001 - the output boundary must catch everything
        error = YojoError(
            "internal_error",
            "yojo hit an unexpected internal error.",
            "If the command was a write, inspect the remote item before running it again. "
            "Report the error_type to the yojo maintainers if it repeats.",
            {"error_type": type(exc).__name__},
        )
    return error.envelope(), int(error.exit_code)


def main(argv: list[str] | None = None, runtime: Runtime | None = None) -> int:
    rt = runtime or default_runtime()
    tokens = sys.argv[1:] if argv is None else list(argv)
    options = tokens[: tokens.index("--")] if "--" in tokens else tokens
    try:
        is_tty = bool(rt.stdout_isatty())
    except (OSError, ValueError):
        is_tty = False
    rt.json_mode = "--json" in options or not is_tty
    envelope, exit_code = outcome(tokens, rt)
    try:
        if rt.json_mode:
            text = json.dumps(envelope, ensure_ascii=True, separators=(",", ":")) + "\n"
        else:
            text = render(envelope, command_path(tokens), Style(color_enabled(rt.environ)))
        rt.stdout.write(text)
        rt.stdout.flush()
    except (BrokenPipeError, OSError):
        pass
    return exit_code
