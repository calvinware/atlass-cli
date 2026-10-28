"""Run the README demo command through the real `yojo` CLI against an in-memory Jira.

`scripts/capture_demos.py` puts a `yojo` shim on PATH that runs this file, so the recorded
terminal shows the literal command while the parser, handlers, and renderer are the real ones.
Requests go to the test fixture server, the token lives in an in-memory keyring, and the working
directory is an empty temporary folder: no network, no OS keyring, no yojo.yaml. Any other
command line is refused, so the shim cannot be used for anything but the demo.

Set YOJO_DEMO_REQUEST_LOG to a file path to record the `[method, path]` of every request.
"""

from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.conftest import (  # noqa: E402
    JIRA,
    JIRA_TOKEN,
    FakeKeyring,
    FakeServer,
    fixture,
    jira_path,
)
from yojo.auth import Credential, CredentialStore  # noqa: E402
from yojo.cli import Runtime, main  # noqa: E402

DEMO_ARGV = (
    "jira",
    "issue",
    "create",
    "PROJ",
    "--type",
    "Bug",
    "--summary",
    "Fix example sign-in",
    "--base-url",
    JIRA,
)
# The command line as typed in the tape and shown in the README.
DEMO_COMMAND = (
    'yojo jira issue create PROJ --type Bug --summary "Fix example sign-in" --base-url ' + JIRA
)
NEW_KEY = "PROJ-128"
# The preflight reads, the single create, and the read-back that confirms it.
EXPECTED_REQUESTS = [
    ["GET", jira_path("/rest/api/2/issue/createmeta/PROJ/issuetypes")],
    ["GET", jira_path("/rest/api/2/issue/createmeta/PROJ/issuetypes/10002")],
    ["POST", jira_path("/rest/api/2/issue")],
    ["GET", jira_path(f"/rest/api/2/issue/{NEW_KEY}")],
]
PASSED_ENV = ("TERM", "NO_COLOR", "COLUMNS")


class DemoRefused(Exception):
    """The shim only runs the documented demo command."""


def demo_server() -> FakeServer:
    """A Jira whose Bug screen needs only a summary, so the demo is one call with no config."""
    bug_fields: dict[str, Any] = copy.deepcopy(fixture("createmeta_bug_fields.json"))
    for value in bug_fields["values"]:
        if value["fieldId"] in ("customfield_10500", "customfield_10600"):
            value["required"] = False
    created: dict[str, Any] = fixture("issue_todo.json")
    created.update(id="30128", key=NEW_KEY, self=f"{JIRA}/rest/api/2/issue/{NEW_KEY}")
    created["fields"].update(
        summary="Fix example sign-in",
        project={"key": "PROJ", "id": "10100"},
        issuetype={"name": "Bug", "id": "10002"},
    )
    server = FakeServer()
    server.add(
        "GET",
        jira_path("/rest/api/2/issue/createmeta/PROJ/issuetypes"),
        fixture("createmeta_issuetypes.json"),
    )
    server.add("GET", jira_path("/rest/api/2/issue/createmeta/PROJ/issuetypes/10002"), bug_fields)
    server.add(
        "POST",
        jira_path("/rest/api/2/issue"),
        {"id": "30128", "key": NEW_KEY, "self": f"{JIRA}/rest/api/2/issue/{NEW_KEY}"},
        status=201,
    )
    server.add("GET", jira_path(f"/rest/api/2/issue/{NEW_KEY}"), created)
    return server


def run_demo(
    argv: Sequence[str],
    stdout: TextIO,
    stderr: TextIO,
    *,
    stdout_isatty: bool,
    environ: Mapping[str, str],
) -> tuple[int, FakeServer]:
    if tuple(argv) != DEMO_ARGV:
        raise DemoRefused("this shim runs only: yojo " + " ".join(DEMO_ARGV))
    server = demo_server()
    keyring = FakeKeyring()
    CredentialStore(keyring).save("jira", JIRA, Credential(auth="pat", secret=JIRA_TOKEN))
    with tempfile.TemporaryDirectory() as tmp:
        runtime = Runtime(
            stdin=io.BytesIO(),
            stdout=stdout,
            stderr=stderr,
            cwd=Path(tmp),
            stdin_isatty=lambda: False,
            read_hidden=lambda: "",
            store_factory=lambda: CredentialStore(keyring),
            transport=server.transport(),
            stdout_isatty=lambda: stdout_isatty,
            environ={k: v for k, v in environ.items() if k in PASSED_ENV},
        )
        code = main(list(argv), runtime)
    return code, server


def request_log(server: FakeServer) -> list[list[str]]:
    return [[r.method, r.url.path] for r in server.requests]


def cli(argv: Sequence[str]) -> int:
    try:
        code, server = run_demo(
            argv,
            sys.stdout,
            sys.stderr,
            stdout_isatty=sys.stdout.isatty(),
            environ=os.environ,
        )
    except DemoRefused as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    log = os.environ.get("YOJO_DEMO_REQUEST_LOG")
    if log:
        Path(log).write_text(json.dumps(request_log(server)), encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
