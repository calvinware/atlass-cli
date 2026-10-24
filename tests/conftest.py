from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator

from atlass_cli.auth import KEYRING_SERVICE, Credential, CredentialStore, account_name
from atlass_cli.cli import Runtime, main
from atlass_cli.contracts import COMMANDS_BY_PATH, command_schema

FIXTURES = Path(__file__).parent / "fixtures"
# Values that would run a command or split arguments if a shell interpreted them.
SHELL_SENTINELS = [
    "$(touch sub-ran)",
    "a; touch semi-ran",
    "a & touch amp-ran",
    "a | touch pipe-ran",
    'Won\'t "do" $HOME %PATH% ^&',
]
JIRA = "https://jira.example.test/jira"
CONFLUENCE = "https://confluence.example.test/confluence"
JIRA_TOKEN = "jira-secret-token-123"
CONFLUENCE_TOKEN = "confluence-secret-token-456"

BASE_CONFIG = """\
version: 1
jira:
  base_url: https://jira.example.test/jira/
  max_attachment_bytes: {max_bytes}
  projects:
    - key: EXAMPLE
      allowed_issue_types: [Story, Feature]
      story_type: Story
      feature_type: Feature
      feature_create_policy: {policy}
      fields:
        acceptance_criteria: {{jira_name: Acceptance Criteria, encoding: text}}
        feature_link: {{jira_name: Parent Link, encoding: issue_key_string}}
        work_started: {{jira_name: Work Started, encoding: single_option}}
      create:
        story:
          required: [acceptance_criteria, feature_link]
        feature:
          required: []
      transitions:
        in_progress:
          from: [To Do]
          to: In Progress
          pre_edit: {pre_edit}
          auto_set: {{work_started: {work_started}}}
          require: []
        done:
          from: [In Progress]
          to: Done
          require:
            - kind: attachment_filename
              pattern: "{{key}}_TestResults_*"
confluence:
  base_url: https://confluence.example.test/confluence
"""


def config_text(
    *,
    max_bytes: int = 52428800,
    policy: str = "human_confirmed",
    pre_edit: str = "false",
    work_started: str = "Yes",
) -> str:
    return BASE_CONFIG.format(
        max_bytes=max_bytes, policy=policy, pre_edit=pre_edit, work_started=work_started
    )


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


Responder = Callable[[httpx.Request], httpx.Response]


@dataclass
class FakeServer:
    """Routes (method, path) to queued responses; the last queued response repeats."""

    routes: dict[tuple[str, str], list[Responder]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def add(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        handler: Responder | None = None,
    ) -> FakeServer:
        def respond(request: httpx.Request) -> httpx.Response:
            if body is None:
                return httpx.Response(status, headers=headers)
            return httpx.Response(status, json=body, headers=headers)

        self.routes.setdefault((method, path), []).append(handler or respond)
        return self

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.routes.get((request.method, request.url.path))
        if not queue:
            return httpx.Response(404, json={"errorMessages": [f"no route {request.url.path}"]})
        responder = queue.pop(0) if len(queue) > 1 else queue[0]
        return responder(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def writes(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method not in ("GET", "HEAD")]

    def paths(self, method: str | None = None) -> list[str]:
        return [r.url.path for r in self.requests if method is None or r.method == method]


class FakeKeyring:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.data.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.data[(service, username)] = password


@dataclass
class Result:
    code: int
    out: dict[str, Any]
    stdout: str
    stderr: str

    @property
    def data(self) -> Any:
        assert self.out["ok"] is True, self.out
        return self.out["data"]

    @property
    def error(self) -> dict[str, Any]:
        assert self.out["ok"] is False, self.out
        return dict(self.out["error"])


def _command_path(argv: list[str]) -> str | None:
    words = [w for w in argv if not w.startswith("-")]
    for n in range(len(words), 0, -1):
        if " ".join(words[:n]) in COMMANDS_BY_PATH:
            return " ".join(words[:n])
    return None


@dataclass
class Harness:
    tmp_path: Path
    server: FakeServer
    keyring: FakeKeyring

    def write_config(self, text: str | None = None) -> Path:
        path = self.tmp_path / "atl.yaml"
        path.write_text(text if text is not None else config_text())
        return path

    def store(self, service: str = "jira", token: str = JIRA_TOKEN) -> None:
        base = JIRA if service == "jira" else CONFLUENCE
        CredentialStore(self.keyring).save(service, base, Credential(auth="pat", secret=token))

    def stored(self, service: str) -> str | None:
        base = JIRA if service == "jira" else CONFLUENCE
        return self.keyring.get_password(KEYRING_SERVICE, account_name(service, base))

    def run(
        self,
        *argv: str,
        stdin: bytes = b"",
        tty: bool = False,
        hidden: list[str] | None = None,
        store_factory: Callable[[], CredentialStore] | None = None,
    ) -> Result:
        stdout, stderr = io.StringIO(), io.StringIO()
        secrets = list(hidden or [])
        runtime = Runtime(
            stdin=io.BytesIO(stdin),
            stdout=stdout,
            stderr=stderr,
            cwd=self.tmp_path,
            stdin_isatty=lambda: tty,
            read_hidden=lambda: secrets.pop(0),
            store_factory=store_factory or (lambda: CredentialStore(self.keyring)),
            transport=self.server.transport(),
        )
        code = main(list(argv), runtime)
        text = stdout.getvalue()
        lines = text.splitlines()
        assert len(lines) == 1, f"expected exactly one JSON line on stdout, got: {text!r}"
        out = json.loads(lines[0])
        assert isinstance(out, dict)
        assert "Traceback" not in stderr.getvalue()
        path = _command_path(list(argv))
        if path is not None and "--help" not in argv and "-h" not in argv:
            schema = command_schema(COMMANDS_BY_PATH[path])
            Draft202012Validator(schema["success" if out["ok"] else "error"]).validate(out)
        return Result(code=code, out=out, stdout=text, stderr=stderr.getvalue())


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path=tmp_path, server=FakeServer(), keyring=FakeKeyring())


def jira_path(path: str) -> str:
    return "/jira" + path


def confluence_path(path: str) -> str:
    return "/confluence" + path


def backticked(text: str, prefix: str) -> list[str]:
    return [c for c in re.findall(r"`([^`]*)`", text) if c.startswith(prefix)]


def run_in_posix_shell(command: str, cwd: Path) -> list[str]:
    """Run ``command`` in a real POSIX shell with ``atl`` stubbed to print its arguments."""
    sh = shutil.which("sh")
    if sh is None or sys.platform == "win32":
        pytest.skip("needs a POSIX shell")
    stub = 'atl() { printf "%s\\0" atl "$@"; }\n'
    # The command under test is atl's own fix text; the shell must keep its values literal.
    out = subprocess.run(  # noqa: S603 - executing quoted recovery text is the point of the test
        [sh, "-c", stub + command], cwd=cwd, capture_output=True, check=True
    )
    assert sorted(os.listdir(cwd)) == [], "the shell ran an injected command"
    return out.stdout.decode().split("\0")[:-1]


def run_argv(argv: list[str]) -> list[str]:
    """Pass ``argv[1:]`` to a real process without a shell and return what it received."""
    script = "import json, sys; print(json.dumps(sys.argv[1:]))"
    out = subprocess.run(  # noqa: S603 - no shell; argv is passed through literally
        [sys.executable, "-c", script, *argv[1:]], capture_output=True, check=True
    )
    return [argv[0], *json.loads(out.stdout)]
