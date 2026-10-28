"""Capture the exact output of every documented command example, in both output faces.

Each example in docs/examples/commands.json runs through the real parser, handlers, and renderer
against the test fixture server and an in-memory keyring, in a temporary folder: no network, no OS
keyring, no real token. Every call runs twice from a fresh copy of the same fixture scenario: once
on a terminal with NO_COLOR=1, and once with `--json` and stdout piped. The exact stdout, exit
code, and fixture requests of both runs are written to docs/examples/captured-output.json, which
scripts/generate_command_reference.py renders into docs/reference/commands.md.

    python scripts/capture_command_examples.py --write   # regenerate captured-output.json
    python scripts/capture_command_examples.py --check   # exit 1 if the committed file drifts
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.conftest import (  # noqa: E402
    BITBUCKET,
    BITBUCKET_TOKEN,
    CONFLUENCE,
    CONFLUENCE_TOKEN,
    JIRA,
    JIRA_TOKEN,
    FakeKeyring,
    FakeServer,
    Harness,
    bitbucket_path,
    config_text,
    confluence_path,
    fixture,
    jira_path,
)
from yojo.auth import ensure_native_backend  # noqa: E402
from yojo.cli import main  # noqa: E402
from yojo.render import Style, render  # noqa: E402

EXAMPLES_PATH = ROOT / "docs" / "examples" / "commands.json"
CAPTURED_PATH = ROOT / "docs" / "examples" / "captured-output.json"
# The temporary folder each capture runs in is shown as this path, so no machine path is published.
WORKDIR = "/work/repo"
SECRETS = (JIRA_TOKEN, CONFLUENCE_TOKEN, BITBUCKET_TOKEN, "Bearer ", "Basic ")
SAFE_HOST_SUFFIX = ".example.test"
TERMINAL_ENVIRON = {"NO_COLOR": "1", "TERM": "xterm-256color"}


class CaptureError(Exception):
    """A scenario did not behave as its example claims; the message says which and how."""


@dataclass
class Call:
    """Terminal state for one run; the fixture server and keyring live on the harness."""

    stdin_tty: bool = False
    hidden: list[str] = field(default_factory=list)
    store_factory: Callable[[], Any] | None = None


Scenario = Callable[[Harness], Call | None]


# --------------------------------------------------------------------------- fixture helpers


def jira_config(harness: Harness, text: str | None = None) -> None:
    harness.write_config(text if text is not None else config_text())
    harness.store("jira")


def issue(key: str, **fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = fixture("issue_todo.json")
    body["key"] = key
    body["fields"].update(fields)
    return body


def permissions(**granted: bool) -> dict[str, Any]:
    return {"permissions": {k: {"key": k, "havePermission": v} for k, v in granted.items()}}


def bitbucket_page(values: list[Any], *, start: int = 0, next_start: int | None = None) -> Any:
    body: dict[str, Any] = {
        "size": len(values),
        "limit": 25,
        "start": start,
        "isLastPage": next_start is None,
        "values": values,
    }
    if next_start is not None:
        body["nextPageStart"] = next_start
    return body


def bitbucket_config(harness: Harness) -> None:
    harness.write_config(f"version: 1\nbitbucket:\n  base_url: {BITBUCKET}\n")
    harness.store("bitbucket", BITBUCKET_TOKEN)


def confluence_config(harness: Harness) -> None:
    harness.write_config(f"version: 1\nconfluence:\n  base_url: {CONFLUENCE}\n")
    harness.store("confluence", CONFLUENCE_TOKEN)


def page_body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = fixture("confluence_page.json")
    body.update(
        title="Release checklist",
        space={"key": "DOCS", "name": "Documentation"},
        body={
            "storage": {
                "value": "<p>Keep related decisions and links together.</p>",
                "representation": "storage",
            }
        },
    )
    body.update(changes)
    return body


# --------------------------------------------------------------------------- scenarios


def offline(_: Harness) -> None:
    return None


def login_ok(harness: Harness) -> Call:
    harness.write_config(f"version: 1\njira:\n  base_url: {JIRA}\n")
    harness.server.add("GET", jira_path("/rest/api/2/myself"), {"name": "example-user"})
    return Call(stdin_tty=True, hidden=[JIRA_TOKEN])


def login_plaintext_keyring(harness: Harness) -> Call:
    harness.write_config(f"version: 1\njira:\n  base_url: {JIRA}\n")

    def plaintext_only() -> Any:
        backend = type("PlaintextKeyring", (), {"__module__": "keyrings.alt.file"})()
        ensure_native_backend(backend)
        raise CaptureError("a plaintext keyring backend was accepted")

    return Call(stdin_tty=True, hidden=[JIRA_TOKEN], store_factory=plaintext_only)


def jira_metadata(harness: Harness) -> None:
    server = harness.server
    server.add("GET", jira_path("/rest/api/2/field"), fixture("fields.json"))
    server.add("GET", jira_path("/rest/api/2/project/EXAMPLE"), fixture("jira_project.json"))
    server.add(
        "GET",
        jira_path("/rest/api/2/project/EXAMPLE/statuses"),
        [{"name": "Bug", "statuses": [{"name": n} for n in ("To Do", "In Progress", "Done")]}],
    )


def config_check_ok(harness: Harness) -> None:
    jira_config(harness)
    jira_metadata(harness)
    harness.server.add("GET", jira_path("/rest/api/2/myself"), {"name": "example-user"})


def config_check_drift(harness: Harness) -> None:
    config_check_ok(harness)
    harness.write_config(
        config_text().replace("{jira_name: Review Flag}", "{jira_name: Review Flg}")
    )


def init_ok(harness: Harness) -> None:
    harness.store("jira")
    server = harness.server
    server.add("GET", jira_path("/rest/api/2/serverInfo"), {"version": "9.12.4"})
    server.add("GET", jira_path("/rest/api/2/myself"), {"name": "example-user"})
    server.add(
        "GET",
        jira_path("/rest/api/2/project/PROJ"),
        {"key": "PROJ", "name": "Example Project", "id": "1"},
    )
    server.add(
        "GET",
        jira_path("/rest/api/2/project/PROJ/statuses"),
        [{"name": "Bug", "statuses": [{"name": n} for n in ("To Do", "In Progress", "Done")]}],
    )
    server.add(
        "GET",
        jira_path("/rest/api/2/issue/createmeta/PROJ/issuetypes"),
        fixture("createmeta_issuetypes.json"),
    )
    server.add("GET", jira_path("/rest/api/2/field"), fixture("fields.json"))


def init_no_login(harness: Harness) -> None:
    init_ok(harness)
    harness.keyring.data.clear()


def agents_conflict(harness: Harness) -> None:
    (harness.tmp_path / "AGENTS.md").write_text("# Team notes\n\n<!-- yojo:end -->\n")


CREATEMETA = jira_path("/rest/api/2/issue/createmeta/EXAMPLE/issuetypes")
BUG_FIELDS = jira_path("/rest/api/2/issue/createmeta/EXAMPLE/issuetypes/10002")


def create_ok(harness: Harness) -> None:
    jira_config(harness)
    server = harness.server
    server.add("GET", CREATEMETA, fixture("createmeta_issuetypes.json"))
    server.add("GET", BUG_FIELDS, fixture("createmeta_bug_fields.json"))
    server.add(
        "POST", jira_path("/rest/api/2/issue"), {"id": "30001", "key": "EXAMPLE-42"}, status=201
    )
    server.add(
        "GET",
        jira_path("/rest/api/2/issue/EXAMPLE-42"),
        issue("EXAMPLE-42", summary="Login button misaligned"),
    )


def get_ok(harness: Harness) -> None:
    jira_config(harness)
    harness.server.add("GET", jira_path("/rest/api/2/field"), fixture("fields.json"))
    harness.server.add(
        "GET",
        jira_path("/rest/api/2/issue/EXAMPLE-42"),
        issue("EXAMPLE-42", customfield_10002=3, priority={"id": "2", "name": "High"}),
    )


def update_ok(harness: Harness) -> None:
    jira_config(harness)
    server = harness.server
    key = jira_path("/rest/api/2/issue/EXAMPLE-42")
    server.add("GET", key + "/editmeta", fixture("editmeta_bug.json"))
    server.add("PUT", key, status=204)
    server.add(
        "GET",
        key,
        issue(
            "EXAMPLE-42",
            priority={"id": "1", "name": "Highest"},
            labels=["login", "ui"],
        ),
    )


def link_ok(harness: Harness) -> None:
    jira_config(harness)
    server = harness.server
    linked = {"id": "1", "type": {"id": "10000"}, "outwardIssue": {"key": "EXAMPLE-43"}}
    server.add("GET", jira_path("/rest/api/2/issue/EXAMPLE-42"), issue("EXAMPLE-42", issuelinks=[]))
    server.add(
        "GET", jira_path("/rest/api/2/issue/EXAMPLE-42"), issue("EXAMPLE-42", issuelinks=[linked])
    )
    server.add("GET", jira_path("/rest/api/2/issue/EXAMPLE-43"), issue("EXAMPLE-43", issuelinks=[]))
    server.add("GET", jira_path("/rest/api/2/issueLinkType"), fixture("issue_link_types.json"))
    server.add("GET", jira_path("/rest/api/2/mypermissions"), permissions(LINK_ISSUES=True))
    server.add("POST", jira_path("/rest/api/2/issueLink"), status=201)


TRANSITION_KEY = "EXAMPLE-42"


def workflow_issue(
    harness: Harness, source: str = "issue_todo.json", attachments: list[str] | None = None
) -> str:
    jira_config(harness)
    server = harness.server
    path = jira_path(f"/rest/api/2/issue/{TRANSITION_KEY}")
    body = fixture(source)
    body["key"] = TRANSITION_KEY
    if attachments is not None:
        body["fields"]["attachment"] = [
            {"id": str(n), "filename": name, "size": 10} for n, name in enumerate(attachments, 1)
        ]
    server.add("GET", path, body)
    transitions = (
        "transitions_todo.json" if source == "issue_todo.json" else "transitions_in_progress.json"
    )
    server.add("GET", path + "/transitions", fixture(transitions))
    server.add("GET", path + "/editmeta", fixture("editmeta.json"))
    return path


def requirements_ok(harness: Harness) -> None:
    workflow_issue(harness)


def requirements_missing(harness: Harness) -> None:
    jira_config(harness)


def transition_ok(harness: Harness) -> None:
    path = workflow_issue(harness, "issue_in_progress.json", ["EXAMPLE-42_Evidence_2026-10-24.xml"])
    harness.server.add("POST", path + "/transitions", status=204)
    done = fixture("issue_in_progress.json")
    done["key"] = TRANSITION_KEY
    done["fields"]["status"] = {"name": "Done"}
    harness.server.add("GET", path, done)


def transition_no_evidence(harness: Harness) -> None:
    workflow_issue(harness, "issue_in_progress.json", ["notes.txt"])


EVIDENCE = "EXAMPLE-42_Evidence_2026-10-24.xml"


def attachment_ok(harness: Harness) -> None:
    jira_config(harness)
    (harness.tmp_path / EVIDENCE).write_bytes(b"<evidence/>\n")
    server = harness.server
    server.add("GET", jira_path("/rest/api/2/issue/EXAMPLE-42"), issue("EXAMPLE-42"))
    server.add("GET", jira_path("/rest/api/2/mypermissions"), permissions(CREATE_ATTACHMENTS=True))
    server.add(
        "GET", jira_path("/rest/api/2/attachment/meta"), {"enabled": True, "uploadLimit": 10485760}
    )

    def uploaded(request: httpx.Request) -> httpx.Response:
        request.read()
        return httpx.Response(200, json=[{"id": "10200", "filename": EVIDENCE, "size": 12}])

    server.add("POST", jira_path("/rest/api/2/issue/EXAMPLE-42/attachments"), handler=uploaded)


def attachment_too_large(harness: Harness) -> None:
    jira_config(harness, config_text(max_bytes=1024))
    (harness.tmp_path / EVIDENCE).write_bytes(b"x" * 2048)


def comment_ok(harness: Harness, *, allowed: bool = True) -> None:
    jira_config(harness)
    server = harness.server
    server.add("GET", jira_path("/rest/api/2/issue/EXAMPLE-42"), issue("EXAMPLE-42"))
    server.add("GET", jira_path("/rest/api/2/mypermissions"), permissions(ADD_COMMENTS=allowed))
    server.add(
        "POST",
        jira_path("/rest/api/2/issue/EXAMPLE-42/comment"),
        {"id": "10100", "body": "Fixed in build 2026.10.24; ready for review."},
        status=201,
    )


def comment_forbidden(harness: Harness) -> None:
    comment_ok(harness, allowed=False)


def search_ok(harness: Harness) -> None:
    jira_config(harness)
    harness.server.add(
        "GET",
        jira_path("/rest/api/2/search"),
        {
            "startAt": 0,
            "maxResults": 2,
            "total": 3,
            "issues": [
                {
                    "key": "EXAMPLE-42",
                    "fields": {
                        "summary": "Login button misaligned",
                        "status": {"name": "In Progress"},
                    },
                },
                {
                    "key": "EXAMPLE-40",
                    "fields": {
                        "summary": "Sign-in page loads slowly",
                        "status": {"name": "In Progress"},
                    },
                },
            ],
        },
    )


def search_bad_project(harness: Harness) -> None:
    jira_config(harness)
    harness.server.add(
        "GET",
        jira_path("/rest/api/2/search"),
        {"errorMessages": ["The value 'NOPE' does not exist for the field 'project'."]},
        status=400,
    )


def page_read_ok(harness: Harness) -> None:
    confluence_config(harness)
    harness.server.add("GET", confluence_path("/rest/api/content/123456"), page_body())


def page_read_missing(harness: Harness) -> None:
    confluence_config(harness)


def page_search_ok(harness: Harness) -> None:
    confluence_config(harness)
    harness.server.add(
        "GET",
        confluence_path("/rest/api/content/search"),
        {
            "start": 0,
            "limit": 10,
            "size": 1,
            "results": [page_body(id="123457", title="Release 1.4 notes")],
            "_links": {},
        },
    )


def page_search_bad_cql(harness: Harness) -> None:
    confluence_config(harness)
    harness.server.add(
        "GET",
        confluence_path("/rest/api/content/search"),
        {"statusCode": 400, "message": "Could not parse cql : title ~"},
        status=400,
    )


def page_create_ok(harness: Harness) -> None:
    confluence_config(harness)
    created = page_body(
        id="123457",
        title="Release 1.4 notes",
        version={"number": 1},
        body={
            "storage": {
                "value": "<p>Release 1.4 adds single sign-on.</p>",
                "representation": "storage",
            }
        },
    )
    harness.server.add("POST", confluence_path("/rest/api/content"), created)
    harness.server.add("GET", confluence_path("/rest/api/content/123457"), created)


def page_update_ok(harness: Harness, *, current: int = 3) -> None:
    confluence_config(harness)
    path = confluence_path("/rest/api/content/123456")
    harness.server.add("GET", path, page_body(version={"number": current}))
    harness.server.add("PUT", path, page_body(version={"number": current + 1}))
    harness.server.add("GET", path, page_body(version={"number": current + 1}))


def page_update_conflict(harness: Harness) -> None:
    page_update_ok(harness, current=4)


REPO = bitbucket_path("/projects/PROJ/repos/web")


def repo_list_ok(harness: Harness) -> None:
    bitbucket_config(harness)
    repos = []
    for slug in ("api", "web"):
        body = fixture("bitbucket_repo.json")
        body["slug"] = body["name"] = slug
        repos.append(body)
    harness.server.add("GET", bitbucket_path("/projects/PROJ/repos"), bitbucket_page(repos))


def repo_list_bad_cursor(harness: Harness) -> None:
    bitbucket_config(harness)
    body = bitbucket_page([fixture("bitbucket_repo.json")])
    body["isLastPage"] = False
    harness.server.add("GET", bitbucket_path("/projects/PROJ/repos"), body)


def repo_get_ok(harness: Harness) -> None:
    bitbucket_config(harness)
    harness.server.add("GET", REPO, fixture("bitbucket_repo.json"))
    harness.server.add(
        "GET", REPO + "/default-branch", {"id": "refs/heads/main", "displayId": "main"}
    )


def bitbucket_only(harness: Harness) -> None:
    bitbucket_config(harness)


def branches(request: httpx.Request) -> httpx.Response:
    wanted = request.url.params.get("filterText", "").casefold()
    values = [b for b in fixture("bitbucket_branches.json")["values"] if wanted in b["displayId"]]
    return httpx.Response(200, json=bitbucket_page(values))


def branch_list_ok(harness: Harness) -> None:
    bitbucket_config(harness)
    harness.server.add("GET", REPO + "/branches", handler=branches)


def pr_list_ok(harness: Harness) -> None:
    bitbucket_config(harness)
    harness.server.add(
        "GET", REPO + "/pull-requests", bitbucket_page([fixture("bitbucket_pull_request.json")])
    )


def pr_get_ok(harness: Harness) -> None:
    bitbucket_config(harness)
    harness.server.add("GET", REPO + "/pull-requests/42", fixture("bitbucket_pull_request.json"))


def pr_diff_ok(harness: Harness) -> None:
    pr_get_ok(harness)
    diff = fixture("bitbucket_diff.json")
    diff["diffs"] = [
        d for d in diff["diffs"] if (d.get("destination") or {}).get("toString") == "src/app.py"
    ]
    harness.server.add("GET", REPO + "/pull-requests/42/diff/src/app.py", diff)


def created_pr(request: httpx.Request) -> httpx.Response:
    sent = json.loads(request.content)
    body = fixture("bitbucket_pull_request.json")
    body["title"] = sent["title"]
    body["description"] = sent.get("description")
    return httpx.Response(201, json=body)


def pr_create_ok(harness: Harness) -> None:
    bitbucket_config(harness)
    server = harness.server
    server.add("GET", REPO, fixture("bitbucket_repo.json"))
    server.add("GET", REPO + "/branches", handler=branches)
    server.add("GET", REPO + "/pull-requests", bitbucket_page([]))
    server.add("POST", REPO + "/pull-requests", handler=created_pr)


def pr_comment_ok(harness: Harness, *, duplicate: bool = False) -> None:
    bitbucket_config(harness)
    server = harness.server
    pr = REPO + "/pull-requests/42"
    activities = fixture("bitbucket_activities.json")
    if duplicate:
        activities["values"][0]["comment"]["text"] = "Ready for review."
    server.add("GET", pr, fixture("bitbucket_pull_request.json"))
    server.add("GET", pr + "/activities", activities)
    server.add("POST", pr + "/comments", fixture("bitbucket_comment.json"), status=201)
    server.add("GET", pr + "/comments/777", fixture("bitbucket_comment.json"))


def pr_comment_duplicate(harness: Harness) -> None:
    pr_comment_ok(harness, duplicate=True)


# Success and error scenario for each command, in registry order.
SCENARIOS: dict[str, tuple[Scenario, Scenario]] = {
    "commands": (offline, offline),
    "schema": (offline, offline),
    "login": (login_ok, login_plaintext_keyring),
    "config check": (config_check_ok, config_check_drift),
    "init": (init_ok, init_no_login),
    "setup": (offline, offline),
    "agent print": (offline, offline),
    "agent install": (offline, agents_conflict),
    "jira issue create": (create_ok, create_ok),
    "jira issue get": (get_ok, get_ok),
    "jira issue update": (update_ok, update_ok),
    "jira issue link": (link_ok, link_ok),
    "jira requirements": (requirements_ok, requirements_missing),
    "jira transition": (transition_ok, transition_no_evidence),
    "jira attachment add": (attachment_ok, attachment_too_large),
    "jira comment add": (comment_ok, comment_forbidden),
    "jira search": (search_ok, search_bad_project),
    "confluence page read": (page_read_ok, page_read_missing),
    "confluence page search": (page_search_ok, page_search_bad_cql),
    "confluence page create": (page_create_ok, page_create_ok),
    "confluence page update": (page_update_ok, page_update_conflict),
    "bitbucket repo list": (repo_list_ok, repo_list_bad_cursor),
    "bitbucket repo get": (repo_get_ok, bitbucket_only),
    "bitbucket branch list": (branch_list_ok, bitbucket_only),
    "bitbucket pr list": (pr_list_ok, bitbucket_only),
    "bitbucket pr get": (pr_get_ok, bitbucket_only),
    "bitbucket pr diff": (pr_diff_ok, bitbucket_only),
    "bitbucket pr create": (pr_create_ok, pr_create_ok),
    "bitbucket pr comment": (pr_comment_ok, pr_comment_duplicate),
}
# `setup` opens the terminal app instead of printing; only its no-terminal fallback is captured.
INTERACTIVE = frozenset({"setup"})


# --------------------------------------------------------------------------- running


def stdin_text(example: dict[str, Any], prefix: str) -> str | None:
    document = example.get(f"{prefix}input")
    if document is not None:
        return json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    text = example.get(f"{prefix}stdin")
    return text if isinstance(text, str) else None


def run_once(
    scenario: Scenario, argv: list[str], stdin: str | None, terminal: bool
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp).resolve()
        harness = Harness(tmp_path=cwd, server=FakeServer(), keyring=FakeKeyring())
        call = scenario(harness) or Call()
        runtime, stdout, stderr = harness.runtime(
            (stdin or "").encode("utf-8"),
            call.stdin_tty,
            call.hidden,
            call.store_factory,
            stdout_tty=terminal,
            environ=dict(TERMINAL_ENVIRON) if terminal else {},
        )
        code = main(argv if terminal else ["--json", *argv], runtime)
        text = stdout.getvalue()
        if "Traceback" in stderr.getvalue():
            raise CaptureError(f"`yojo {' '.join(argv)}` raised:\n{stderr.getvalue()}")
        return {
            "exit": code,
            "stdout": text.replace(str(cwd), WORKDIR),
            "requests": [f"{r.method} {r.url.path}" for r in harness.server.requests],
            "writes": len(harness.server.writes()),
        }


def capture_case(
    path: str, scenario: Scenario, argv: list[str], stdin: str | None
) -> dict[str, Any]:
    piped = run_once(scenario, argv, stdin, terminal=False)
    terminal = None if path in INTERACTIVE else run_once(scenario, argv, stdin, terminal=True)
    case: dict[str, Any] = {"argv": argv, "stdin": stdin}
    case["exit"] = piped["exit"]
    case["requests"] = piped["requests"]
    case["writes"] = piped["writes"]
    case["json"] = piped["stdout"]
    case["terminal"] = None
    if terminal is not None:
        if terminal["exit"] != piped["exit"] or terminal["requests"] != piped["requests"]:
            raise CaptureError(f"{path}: the terminal and --json runs did not make the same calls")
        case["terminal"] = terminal["stdout"]
    return case


def terminal_form(envelope: dict[str, Any]) -> dict[str, Any]:
    """The same envelope as a terminal caller gets it: a retry repeats the caller's own form."""
    details = (envelope.get("error") or {}).get("details")
    if not isinstance(details, dict) or not isinstance(details.get("retry_argv"), list):
        return envelope
    argv = [
        part for n, part in enumerate(details["retry_argv"]) if not (n == 1 and part == "--json")
    ]
    retry = details["retry"].replace("yojo --json ", "yojo ", 1)
    error = {**envelope["error"], "details": {**details, "retry": retry, "retry_argv": argv}}
    return {**envelope, "error": error}


def problems_in(path: str, which: str, case: dict[str, Any]) -> list[str]:
    where = f"{path} ({which})"
    problems = []
    lines = case["json"].splitlines()
    if len(lines) != 1:
        return [f"{where}: --json printed {len(lines)} lines, not 1"]
    envelope = json.loads(lines[0])
    if lines[0] != json.dumps(envelope, ensure_ascii=True, separators=(",", ":")):
        problems.append(f"{where}: --json output is not compact one-line JSON")
    if (which == "success") != (envelope.get("ok") is True):
        problems.append(f"{where}: expected ok={which == 'success'}, got {lines[0][:300]}")
    if (case["exit"] == 0) != (envelope.get("ok") is True):
        problems.append(f"{where}: exit {case['exit']} disagrees with the envelope")
    if which == "error" and case["writes"]:
        problems.append(f"{where}: the failing example sent {case['writes']} write(s)")
    if case["terminal"] is not None:
        if "\x1b" in case["terminal"]:
            problems.append(f"{where}: terminal output has ANSI codes with NO_COLOR=1")
        if case["terminal"] != render(terminal_form(envelope), path, Style(False)):
            problems.append(f"{where}: terminal output is not the rendering of the --json envelope")
    for text in (case["json"], case["terminal"] or ""):
        if any(secret in text for secret in SECRETS):
            problems.append(f"{where}: output contains a token or credential header")
        if tempfile.gettempdir() in text:
            problems.append(f"{where}: output contains a temporary path")
    return problems


def build() -> dict[str, Any]:
    examples = json.loads(EXAMPLES_PATH.read_text(encoding="utf-8"))["commands"]
    missing = sorted(set(examples) ^ set(SCENARIOS))
    if missing:
        raise CaptureError(f"commands.json and SCENARIOS disagree on: {missing}")
    captured: dict[str, Any] = {}
    problems: list[str] = []
    for path, (success, failure) in SCENARIOS.items():
        example = examples[path]
        cases: dict[str, Any] = {"success": None}
        if path not in INTERACTIVE:
            cases["success"] = capture_case(path, success, example["argv"], stdin_text(example, ""))
        cases["error"] = capture_case(
            path, failure, example["error_argv"], stdin_text(example, "error_")
        )
        for which, case in cases.items():
            if case is not None:
                problems += problems_in(path, which, case)
        captured[path] = cases
    if problems:
        raise CaptureError("\n".join(problems))
    return {"commands": captured}


def serialize(captured: dict[str, Any]) -> str:
    return json.dumps(captured, indent=2, ensure_ascii=False) + "\n"


def main_(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="regenerate captured-output.json")
    mode.add_argument("--check", action="store_true", help="fail if the committed file drifts")
    args = parser.parse_args(argv)
    try:
        text = serialize(build())
    except CaptureError as exc:
        sys.stderr.write(f"command example capture failed:\n{exc}\n")
        return 2
    rel = CAPTURED_PATH.relative_to(ROOT).as_posix()
    if args.check:
        current = (
            CAPTURED_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
            if CAPTURED_PATH.exists()
            else ""
        )
        if current != text:
            diff = difflib.unified_diff(
                current.splitlines(keepends=True),
                text.splitlines(keepends=True),
                f"{rel} (committed)",
                f"{rel} (captured)",
            )
            sys.stderr.write("".join(list(diff)[:200]))
            sys.stderr.write(
                "The captured command output is stale. Run "
                "`python scripts/capture_command_examples.py --write`, then "
                "`python scripts/generate_command_reference.py --write`, and review the result.\n"
            )
            return 1
        sys.stdout.write("captured command output is up to date\n")
        return 0
    CAPTURED_PATH.write_text(text, encoding="utf-8", newline="\n")
    sys.stdout.write(f"wrote {rel}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main_())
