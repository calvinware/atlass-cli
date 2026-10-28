"""`yojo setup`: the full-screen, seven-step setup app for a person at a terminal.

The CLI opens it only when stdout and stdin are terminals and ``--json`` was not given. Every
step reuses the non-interactive operations: ``verify_and_store`` (``yojo login``), ``init.build``
and ``init.publish_reviewed`` (``yojo init``), ``agents.install`` (``yojo agent install``), and
``cli.outcome`` for the read-only try. A token is read from a masked field, the field is cleared
at once, and the token lives only in that call's memory until the keyring has it. Remote text is
shown as plain text, never as markup.
"""

from __future__ import annotations

import contextlib
import copy
import difflib
import io
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar, TypeVar

import yaml
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import (
    Button,
    Checkbox,
    ContentSwitcher,
    DataTable,
    Footer,
    Input,
    Label,
    ListItem,
    ListView,
    Select,
    SelectionList,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.selection_list import Selection

from yojo import agents, bitbucket, confluence, files, init, metadata, retry
from yojo.auth import Credential, CredentialStore, validate_secret
from yojo.cli import Runtime, _client, _verify_login, outcome, verify_and_store
from yojo.config import (
    CONFIG_FILENAME,
    MAX_CONFIG_BYTES,
    ServiceConfig,
    StrictSafeLoader,
    discover_config,
    parse_config_file,
    url_problem,
)
from yojo.contracts import FRIENDLY_NAME_PATTERN, PROJECT_KEY_PATTERN, YojoError
from yojo.transport import ServiceClient, sanitize_text

T = TypeVar("T")

SERVICES = ("jira", "confluence", "bitbucket")
LABELS = {"jira": "Jira", "confluence": "Confluence", "bitbucket": "Bitbucket"}
STEPS = (
    "Connect",
    "Log in",
    "Choose projects",
    "Map fields and rules",
    "Review and save",
    "Connect your agent",
    "Try it",
)
TOKEN_HELP = {
    "jira": "Jira: your avatar > Profile > Personal Access Tokens > Create token.",
    "confluence": "Confluence: your avatar > Settings > Personal Access Tokens > Create token.",
    "bitbucket": "Bitbucket: your avatar > Manage account > HTTP access tokens > Create token.",
}
AGENT_LABELS = {
    "agents": "AGENTS.md, read by many coding agents",
    "claude": "Claude Code skill (.claude/skills/yojo/SKILL.md)",
    "cursor": "Cursor rule (.cursor/rules/yojo.mdc)",
    "copilot": "GitHub Copilot (.github/copilot-instructions.md)",
    "gemini": "Gemini CLI (GEMINI.md)",
}
CRUMBS = ("Connect", "Log in", "Choose", "Rules", "Review", "Agent", "Try")
REPO_PAGE = 100
MAX_SHOWN = 200
TRY_LIMIT = "5"
FRIENDLY_RE = re.compile(FRIENDLY_NAME_PATTERN)
PROJECT_KEY_RE = re.compile(PROJECT_KEY_PATTERN)
# Jira section keys the setup app writes; anything else in an existing file is reported dropped.
KEPT_JIRA_KEYS = {"base_url", "ca_bundle", "fields", "projects"}


def clean_lines(value: Any, limit: int = 300) -> str:
    """Text without control characters or terminal escapes, keeping its line breaks."""
    text = value if isinstance(value, str) else str(value)
    return "\n".join(sanitize_text(line, limit) for line in text.splitlines()[:40])


def plain(value: Any, limit: int = 300) -> Content:
    """Remote or user text as literal content: no markup, no control characters."""
    return Content(clean_lines(value, limit))


def _error_text(error: YojoError) -> str:
    # Service and library errors can echo a submitted token, including in a suggested fix.
    # Only stable code-specific guidance is safe to render in the terminal.
    advice = {
        "usage_error": "Use an HTTPS address without embedded credentials.",
        "response_invalid": "Check the service URL and Retry.",
        "network_error": "Check the service address, network, and CA bundle; then Retry.",
        "metadata_incomplete": "Jira metadata is incomplete. Retry the request.",
        "auth_failed": "Check the token with the service, then Retry.",
        "keyring_unavailable": "Set up a native OS keyring, then Retry.",
        "file_invalid": "Choose a regular file path and Refresh the preview.",
        "file_conflict": "The file changed after review. Refresh the preview.",
        "conflict": "Live metadata changed. Refresh the preview.",
    }
    return f"{error.code}: {advice.get(error.code, 'Check the connection and Retry.')}"


# --------------------------------------------------------------------------- draft


@dataclass(frozen=True)
class Probe:
    service: str
    base_url: str
    version: str | None

    @property
    def summary(self) -> str:
        name = LABELS[self.service]
        from urllib.parse import urlsplit

        address = urlsplit(self.base_url)
        place = f"{address.netloc}{address.path}"
        if self.version:
            return f"✓ {name} {self.version} at {place}"
        return f"✓ {name} at {place}; version unavailable. Log in to confirm."


@dataclass(frozen=True)
class Rule:
    """One team rule the person chose, in the yojo.yaml shape it will be written as."""

    kind: str  # alias | project_alias | create | transition
    project: str | None
    name: str
    value: Any
    origin: str = "setup"  # setup | file

    def describe(self) -> str:
        return describe_rule(self)


def _field_phrase(ref: Any) -> str:
    if isinstance(ref, dict):
        if "jira_name" in ref:
            return f'the Jira field "{ref["jira_name"]}"'
        return f"the Jira field with ID {ref.get('id')}"
    return str(ref)


def describe_rule(rule: Rule) -> str:
    """The rule's effect in plain English, as a person reviewing it would want to read it."""
    if rule.kind == "alias":
        return f'"{rule.name}" means {_field_phrase(rule.value)} in every project.'
    if rule.kind == "project_alias":
        return f'In {rule.project}, "{rule.name}" means {_field_phrase(rule.value)}.'
    if rule.kind == "create":
        required = ", ".join(str(f) for f in rule.value.get("required", []))
        return f"In {rule.project}, a new {rule.name} must have: {required}."
    value: dict[str, Any] = rule.value
    sources = " or ".join(str(s) for s in value.get("from", []))
    needs: list[str] = []
    for item in value.get("require", []):
        if item.get("kind") == "field_set":
            needs.append(f"{item.get('field')} to have a value")
        elif item.get("kind") == "attachment_filename":
            needs.append(f"an attachment named like {item.get('pattern')}")
    text = f"In {rule.project}, moving an issue from {sources} to {value.get('to')}"
    text += f" requires {' and '.join(needs)}." if needs else " follows only Jira's own checks."
    auto = value.get("auto_set") or {}
    for name, setting in auto.items():
        shown = ", ".join(setting) if isinstance(setting, list) else setting
        text += f' yojo sets {name} to "{shown}" during the move.'
    if auto and value.get("pre_edit"):
        text += (
            " If a set field is not on the transition screen, yojo first saves it with a "
            "separate edit and then moves the issue: two writes that are not atomic, so the edit "
            "stays if the move fails."
        )
    elif auto:
        text += (
            " If a set field is not on the transition screen, the move is refused before "
            "anything is written."
        )
    return text


@dataclass
class Draft:
    """Everything chosen so far. Secrets are never part of it."""

    urls: dict[str, str] = field(default_factory=dict)
    ca_bundle: str | None = None
    probes: dict[str, Probe] = field(default_factory=dict)
    logins: dict[str, str] = field(default_factory=dict)
    stored: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    repos: list[str] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    output: str = CONFIG_FILENAME
    revision: int = 0
    saved: tuple[str, str] | None = None
    saved_data: bytes | None = None
    saved_revision: int | None = None
    try_revision: int | None = None
    agent_files: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def touch(self) -> None:
        self.revision += 1
        self.try_revision = None

    def connection(self, service: str) -> tuple[str | None, str | None]:
        return self.urls.get(service), self.ca_bundle

    def forget_connection(self, service: str) -> None:
        self.probes.pop(service, None)
        self.logins.pop(service, None)
        if service == "jira":
            self.projects.clear()
            self.rules.clear()
        if service == "bitbucket":
            self.repos.clear()
        self.saved = None
        self.saved_data = None
        self.saved_revision = None
        self.touch()

    def policy(self) -> dict[str, Any] | None:
        """The rules as the ``yojo init --input`` policy object, or None when there are none."""
        rules = [r for r in self.rules if r.kind == "alias" or r.project in self.projects]
        if not rules:
            return None
        aliases: dict[str, Any] = {}
        projects: dict[str, dict[str, Any]] = {}
        for rule in rules:
            if rule.kind == "alias":
                aliases[rule.name] = rule.value
                continue
            assert rule.project is not None
            entry = projects.setdefault(rule.project, {"key": rule.project})
            section = {"project_alias": "fields", "create": "create", "transition": "transitions"}
            entry.setdefault(section[rule.kind], {})[rule.name] = rule.value
        jira: dict[str, Any] = {}
        if aliases:
            jira["fields"] = aliases
        ordered = [projects[k] for k in self.projects if k in projects]
        if ordered:
            jira["projects"] = ordered
        return {"jira": jira}

    def request(self) -> init.InitRequest:
        return init.InitRequest(
            jira_url=self.urls.get("jira"),
            projects=tuple(self.projects) if "jira" in self.urls else (),
            confluence_url=self.urls.get("confluence"),
            bitbucket_url=self.urls.get("bitbucket"),
            ca_bundle=self.ca_bundle,
            repos=tuple(self.repos) if "bitbucket" in self.urls else (),
            output=self.output,
        )


# --------------------------------------------------------------------------- services


@dataclass(frozen=True)
class FieldChoice:
    id: str
    name: str
    ref: str  # what yojo.yaml uses: the visible name, or the ID when the name is shared


@dataclass(frozen=True)
class ProjectMeta:
    key: str
    issue_types: list[str]
    statuses: list[str]


class Services:
    """The setup app's only door to the network, the keyring, and files. Synchronous."""

    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._store: CredentialStore | None = None

    @property
    def cwd(self) -> Path:
        return self.rt.cwd

    def store(self) -> CredentialStore:
        if self._store is None:
            self._store = self.rt.store_factory()
        return self._store

    def config(self, draft: Draft, service: str) -> ServiceConfig:
        bundle = None
        if draft.ca_bundle:
            bundle = files.resolve(self.cwd, draft.ca_bundle)
        return ServiceConfig(draft.urls[service], bundle)

    def _open(self, service: str, cfg: ServiceConfig) -> Any:
        rt = replace(self.rt, json_mode=True, stdout=io.StringIO(), stderr=io.StringIO())
        if self._store is not None:
            store = self._store
            rt = replace(rt, store_factory=lambda: store)
        return _client(rt, service, cfg)

    # Step 1 -------------------------------------------------------------------------------

    def probe(self, service: str, raw_url: str, ca_bundle: str | None) -> Probe:
        url, problem = url_problem(raw_url)
        if url is None:
            raise YojoError(
                "usage_error",
                f"The {LABELS[service]} address {problem}.",
                "Enter the HTTPS address you open in a browser, including any path, for example "
                f"https://{service}.example.com/{service}.",
            )
        bundle = files.resolve(self.cwd, ca_bundle) if ca_bundle else None
        if bundle is not None and not bundle.is_file():
            raise YojoError(
                "usage_error",
                f"The CA bundle {bundle} does not exist.",
                "Enter the path of a readable PEM file, or leave the CA bundle empty.",
            )
        client = ServiceClient(service, url, None, ca_bundle=bundle, transport=self.rt.transport)
        with client:
            if service == "bitbucket":
                return Probe(service, url, bitbucket.application_version(client))
            if service == "confluence":
                return Probe(service, url, confluence.application_version(client))
            try:
                return Probe(service, url, metadata.server_version(client))
            except YojoError as exc:
                if exc.code in ("not_found", "response_invalid", "auth_failed", "forbidden"):
                    return Probe(service, url, None)
                raise

    # Step 2 -------------------------------------------------------------------------------

    def login(self, draft: Draft, service: str, secret: str) -> str:
        credential = Credential(auth="pat", secret=validate_secret(secret, "token"))
        cfg = self.config(draft, service)
        confirmed = verify_and_store(self.rt, self.store(), service, cfg, credential)
        who = self._who(service, confirmed.get("user"), confirmed.get("version"))
        if secret in who:
            # The verifier already saved the credential; report success without echoing it.
            return f"Logged in to {LABELS[service]}."
        return who

    def stored_login(self, draft: Draft, service: str) -> str:
        """Who the stored login belongs to; raises when there is none or it no longer works."""
        cfg = self.config(draft, service)
        with self._open(service, cfg) as client:
            if service == "bitbucket":
                return self._who(service, None, bitbucket.verify_login(client))
            return self._who(service, _verify_login(client, service), None)

    @staticmethod
    def _who(service: str, user: str | None, version: str | None) -> str:
        if user:
            return f"Logged in to {LABELS[service]} as {sanitize_text(user, 100)}."
        return f"Bitbucket {sanitize_text(version, 40)} accepted the token."

    # Step 3 -------------------------------------------------------------------------------

    def projects(self, draft: Draft) -> list[dict[str, str]]:
        with self._open("jira", self.config(draft, "jira")) as client:
            return metadata.projects(client)

    def repos(self, draft: Draft, start: int) -> dict[str, Any]:
        with self._open("bitbucket", self.config(draft, "bitbucket")) as client:
            return bitbucket.repo_list(client, None, start, REPO_PAGE)

    # Step 4 -------------------------------------------------------------------------------

    def fields(self, draft: Draft) -> list[FieldChoice]:
        with self._open("jira", self.config(draft, "jira")) as client:
            known = metadata.all_fields(client)
        if not known or any(
            not fid.strip() or not meta.name.strip() for fid, meta in known.items()
        ):
            raise YojoError(
                "response_invalid",
                "Jira field metadata is incomplete.",
                "Retry loading Jira fields after checking the Jira connection.",
            )
        counts: dict[str, int] = {}
        for meta in known.values():
            counts[meta.name.casefold()] = counts.get(meta.name.casefold(), 0) + 1
        choices = [
            FieldChoice(
                id=fid,
                name=sanitize_text(meta.name, 255),
                ref=meta.name if counts[meta.name.casefold()] == 1 else fid,
            )
            for fid, meta in known.items()
        ]
        return sorted(choices, key=lambda c: (c.name.casefold(), c.id))

    def project_meta(self, draft: Draft, key: str) -> ProjectMeta:
        with self._open("jira", self.config(draft, "jira")) as client:
            types = [t.name for t in metadata.create_issue_types(client, key)]
            statuses = metadata.project_statuses(client, key)
        return ProjectMeta(key, types, statuses)

    # Step 5 -------------------------------------------------------------------------------

    def candidate(self, draft: Draft) -> init.Candidate:
        request = init.normalize(draft.request())
        return init.build(request, draft.policy(), self.cwd, self._open, _verify_login)

    def save(self, candidate: init.Candidate) -> str:
        return init.publish_reviewed(candidate, self.cwd, self._open, _verify_login)

    def existing(self) -> dict[str, Any] | None:
        """The yojo.yaml in this folder as written, after the normal validation."""
        discover_config(self.cwd)  # a lone legacy atl.yaml is refused with its own fix
        path = self.cwd / CONFIG_FILENAME
        if not path.is_file():
            return None
        parse_config_file(path)
        data = files.read_current(path)
        if data is None or len(data) > MAX_CONFIG_BYTES:
            return None
        doc = yaml.load(data.decode("utf-8"), Loader=StrictSafeLoader)  # noqa: S506
        return doc if isinstance(doc, dict) else None

    # Steps 6 and 7 ------------------------------------------------------------------------

    def install_agents(self, targets: list[str]) -> dict[str, Any]:
        return agents.install(targets, self.cwd)

    def try_argv(self, draft: Draft) -> list[str]:
        """The read-only command for the try step, as a person would type it."""
        if draft.projects and "jira" in draft.urls:
            read = [
                "jira",
                "search",
                "--jql",
                f"project = {draft.projects[0]} ORDER BY updated DESC",
            ]
            service = "jira"
        elif draft.repos and "bitbucket" in draft.urls:
            read = ["bitbucket", "pr", "list", "--repo", draft.repos[0]]
            service = "bitbucket"
        elif "jira" in draft.urls:
            read = ["jira", "search", "--jql", "assignee = currentUser() ORDER BY updated DESC"]
            service = "jira"
        elif "confluence" in draft.urls:
            read = ["confluence", "page", "search", "--cql", "type = page"]
            service = "confluence"
        else:
            read = ["bitbucket", "repo", "list"]
            service = "bitbucket"
        argv = [retry.PROGRAM, "--json", *read, "--limit", TRY_LIMIT]
        if draft.saved is None:
            argv += ["--base-url", draft.urls[service]]
        else:
            path = Path(draft.saved[0])
            if path != self.cwd / CONFIG_FILENAME:
                argv += ["--config", str(path)]
        return argv

    def run_read(self, argv: list[str]) -> tuple[dict[str, Any], int]:
        rt = replace(self.rt, json_mode=True, stdout=io.StringIO(), stderr=io.StringIO())
        if self._store is not None:
            store = self._store
            rt = replace(rt, store_factory=lambda: store)
        return outcome(argv[1:], rt)


def _rules_from_file(doc: dict[str, Any]) -> list[Rule]:
    """Rules already in yojo.yaml, kept as written so saving does not drop them silently."""
    jira = doc.get("jira")
    if not isinstance(jira, dict):
        return []
    rules = [
        Rule("alias", None, name, ref, "file") for name, ref in (jira.get("fields") or {}).items()
    ]
    for entry in jira.get("projects") or []:
        key = entry.get("key")
        for name, ref in (entry.get("fields") or {}).items():
            rules.append(Rule("project_alias", key, name, ref, "file"))
        for name, block in (entry.get("create") or {}).items():
            rules.append(Rule("create", key, name, block, "file"))
        for name, rule in (entry.get("transitions") or {}).items():
            rules.append(Rule("transition", key, name, rule, "file"))
    return rules


def draft_from_file(doc: dict[str, Any] | None) -> Draft:
    draft = Draft()
    if not doc:
        return draft
    for service in SERVICES:
        section = doc.get(service)
        if isinstance(section, dict) and isinstance(section.get("base_url"), str):
            draft.urls[service] = section["base_url"]
            if draft.ca_bundle is None and isinstance(section.get("ca_bundle"), str):
                draft.ca_bundle = section["ca_bundle"]
    jira = doc.get("jira")
    if isinstance(jira, dict):
        draft.projects = [p["key"] for p in jira.get("projects") or [] if "key" in p]
        dropped = sorted(set(jira) - KEPT_JIRA_KEYS)
        if dropped:
            draft.notes.append(
                f"Your yojo.yaml sets jira.{', jira.'.join(dropped)}, which setup does not edit; "
                "saving from setup leaves it out. The diff shows it."
            )
    draft.rules = _rules_from_file(doc)
    return draft


def rule_name(to: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", to.casefold()).strip("_")[:50] or "status"
    base = f"to_{base}"
    name, n = base, 2
    while name in taken or not FRIENDLY_RE.fullmatch(name):
        name, n = f"{base}_{n}", n + 1
    return name


def unified_diff(old: bytes | None, new: str, path: str) -> list[str]:
    before = old.decode("utf-8", "replace").splitlines() if old is not None else []
    return list(
        difflib.unified_diff(
            before, new.splitlines(), f"{path} (now)", f"{path} (after save)", lineterm=""
        )
    )


def show_path(path: Path, cwd: Path) -> str:
    try:
        return "./" + path.relative_to(cwd).as_posix()
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------- widgets


class TokenInput(Input):
    """A masked field a token can be pasted into but never copied or cut out of."""

    def action_copy(self) -> None:
        return

    def action_cut(self) -> None:
        return


class StepView(VerticalScroll):
    """One numbered step. ``enter`` runs on arrival; ``blocker`` says why Next is not ready."""

    number: ClassVar[int] = 0

    @property
    def setup(self) -> SetupApp:
        app = self.app
        assert isinstance(app, SetupApp)
        return app

    @property
    def draft(self) -> Draft:
        return self.setup.draft

    def enter(self) -> None:
        pass

    def blocker(self) -> str | None:
        return None

    def lead(self, text: str) -> Static:
        return Static(text, classes="lead", markup=False)

    def say(self, target: str, text: str | Content, kind: str = "info") -> None:
        widget = self.query_one(f"#{target}", Static)
        widget.update(text if isinstance(text, Content) else plain(text, 2000))
        widget.set_classes(f"status -{kind}")


class ConnectStep(StepView):
    number = 1

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.requests: dict[str, object] = {}
        self.requested: dict[str, tuple[str, str | None]] = {}

    def compose(self) -> ComposeResult:
        yield self.lead(
            "Where do your Atlassian tools live? Enter the address you open in a browser, "
            "including any path such as /jira. Leave a service empty to skip it."
        )
        for service in SERVICES:
            with Horizontal(classes="row"):
                yield Label(LABELS[service], classes="field-label")
                yield Input(
                    placeholder=f"https://{service}.example.com/{service}",
                    id=f"url-{service}",
                )
            yield Static("", id=f"probe-{service}", classes="status", markup=False)
        with Horizontal(classes="row"):
            yield Label("CA bundle", classes="field-label")
            yield Input(
                placeholder="optional: PEM file for an internal certificate authority",
                id="ca-bundle",
            )
        yield Button("Check connection", id="check", variant="primary")

    def on_mount(self) -> None:
        for service, url in self.draft.urls.items():
            self.query_one(f"#url-{service}", Input).value = url
        if self.draft.ca_bundle:
            self.query_one("#ca-bundle", Input).value = self.draft.ca_bundle

    def _filled(self) -> dict[str, str]:
        return {
            s: v.strip()
            for s in SERVICES
            if (v := self.query_one(f"#url-{s}", Input).value).strip()
        }

    @on(Input.Changed)
    def _changed(self, event: Input.Changed) -> None:
        if event.input.id and event.input.id.startswith("url-"):
            service = event.input.id.removeprefix("url-")
            if self.requested.get(service) == (
                event.value.strip(),
                self.query_one("#ca-bundle", Input).value.strip() or None,
            ):
                return
            if url_problem(event.value.strip())[0] != self.draft.urls.get(service):
                self.requests.pop(service, None)
                self.requested.pop(service, None)
                self.draft.forget_connection(service)
                self.setup.discard_discovery(service)
                self.say(f"probe-{service}", "")
        elif (
            event.input.id == "ca-bundle" and (event.value.strip() or None) != self.draft.ca_bundle
        ):
            for service in SERVICES:
                if self.requested.get(service) == (
                    self.query_one(f"#url-{service}", Input).value.strip(),
                    event.value.strip() or None,
                ):
                    continue
                self.requests.pop(service, None)
                self.requested.pop(service, None)
                self.draft.forget_connection(service)
                self.setup.discard_discovery(service)
                self.say(f"probe-{service}", "")

    @on(Input.Submitted)
    @on(Button.Pressed, "#check")
    def check(self) -> None:
        filled = self._filled()
        if not filled:
            self.setup.notify("Enter at least one address.", severity="warning")
            return
        ca = self.query_one("#ca-bundle", Input).value.strip() or None
        if ca != self.draft.ca_bundle:
            self.draft.ca_bundle = ca
            for service in SERVICES:
                self.draft.forget_connection(service)
        for service in SERVICES:
            if service not in filled:
                if self.draft.urls.pop(service, None) is not None:
                    self.draft.forget_connection(service)
                self.say(f"probe-{service}", "")
        for service, raw in filled.items():
            self._probe(service, raw, ca)

    def _probe(self, service: str, raw: str, ca: str | None) -> None:
        request = self.requests[service] = object()
        self.requested[service] = (raw, ca)
        self.say(f"probe-{service}", f"Checking {LABELS[service]}...", "busy")
        self.setup.background(
            f"probe-{service}",
            lambda: self.setup.services.probe(service, raw, ca),
            lambda result, error: (
                self._probed(service, raw, ca, result, error)
                if self.requests.get(service) is request
                else None
            ),
        )

    def _probed(
        self,
        service: str,
        raw: str,
        ca: str | None,
        probe: Probe | None,
        error: YojoError | None,
    ) -> None:
        current = self.query_one(f"#url-{service}", Input).value.strip()
        current_ca = self.query_one("#ca-bundle", Input).value.strip() or None
        if current != raw or current_ca != ca:
            return
        if error is not None or probe is None:
            assert error is not None
            self.draft.probes.pop(service, None)
            self.say(f"probe-{service}", f"Not connected. {_error_text(error)}", "error")
            return
        if self.draft.urls.get(service) != probe.base_url:
            self.draft.urls[service] = probe.base_url
            self.draft.logins.pop(service, None)
            self.draft.touch()
        self.draft.probes[service] = probe
        self.say(f"probe-{service}", probe.summary, "ok" if probe.version else "warn")

    def blocker(self) -> str | None:
        filled = self._filled()
        if not filled:
            return "Enter at least one address, then press Check connection."
        current_ca = self.query_one("#ca-bundle", Input).value.strip() or None
        if current_ca != self.draft.ca_bundle:
            return "The CA bundle changed. Check the connection again."
        missing = [
            LABELS[s]
            for s, raw in filled.items()
            if (probe := self.draft.probes.get(s)) is None or url_problem(raw)[0] != probe.base_url
        ]
        if missing:
            return f"Check the connection for {', '.join(missing)} first."
        return None


class LoginStep(StepView):
    number = 2

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.inflight: set[str] = set()

    def compose(self) -> ComposeResult:
        yield self.lead(
            "Log in with a personal access token. yojo checks who you are, then keeps the token "
            "in your operating system's keyring. It is never shown, logged, or written to a file."
        )
        for service in SERVICES:
            with Vertical(id=f"login-{service}", classes="card"):
                yield Static(LABELS[service], classes="card-title", markup=False)
                yield Static(TOKEN_HELP[service], classes="hint", markup=False)
                with Horizontal(classes="row"):
                    yield TokenInput(
                        placeholder="paste your token",
                        password=True,
                        id=f"token-{service}",
                    )
                    yield Button("Log in", id=f"login-button-{service}")
                yield Static("", id=f"who-{service}", classes="status", markup=False)
        yield Static(
            "Basic auth (username and password) is available with `yojo login --auth basic`.",
            classes="hint",
            markup=False,
        )

    def enter(self) -> None:
        for service in SERVICES:
            card = self.query_one(f"#login-{service}")
            card.display = service in self.draft.urls
            if service not in self.draft.urls:
                continue
            title = card.query_one(".card-title", Static)
            title.update(plain(f"{LABELS[service]}  {self.draft.urls[service]}"))
            if service in self.inflight:
                self.say(f"who-{service}", "Checking this login...", "busy")
                continue
            if service in self.draft.logins:
                self.say(f"who-{service}", self.draft.logins[service], "ok")
                continue
            self._look_for_saved(service)
        first = next((s for s in SERVICES if s in self.draft.urls), None)
        if first is not None:
            self.query_one(f"#token-{first}", Input).focus()

    def _look_for_saved(self, service: str) -> None:
        url, ca = self.draft.urls[service], self.draft.ca_bundle
        snapshot = Draft(urls={service: url}, ca_bundle=ca)
        self.say(f"who-{service}", "Looking for a saved login...", "busy")
        self.setup.background(
            f"login-{service}",
            lambda: self.setup.services.stored_login(snapshot, service),
            lambda who, error: self._stored(service, url, ca, who, error),
        )

    def _stored(
        self,
        service: str,
        url: str,
        ca: str | None,
        who: str | None,
        error: YojoError | None,
    ) -> None:
        if self.draft.urls.get(service) != url or self.draft.ca_bundle != ca:
            return
        if who is not None:
            self.draft.logins[service] = who + " (saved login)"
            self.say(f"who-{service}", self.draft.logins[service], "ok")
        elif error is not None and error.code == "auth_missing":
            self.say(f"who-{service}", "Not logged in yet.", "info")
        elif error is not None:
            self.say(
                f"who-{service}", f"The saved login did not work. {_error_text(error)}", "warn"
            )

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        if event.input.id and event.input.id.startswith("token-"):
            self._login(event.input.id.removeprefix("token-"))

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id and event.button.id.startswith("login-button-"):
            self._login(event.button.id.removeprefix("login-button-"))

    def _login(self, service: str) -> None:
        box = self.query_one(f"#token-{service}", Input)
        secret = box.value
        # Clear the field before anything else so the token is not kept in the widget.
        box.value = ""
        if service in self.inflight:
            self.say(f"who-{service}", "A login is still in progress. Wait for its result.", "busy")
            return
        if not secret.strip():
            self.say(f"who-{service}", "Paste a token first.", "warn")
            return
        url, ca = self.draft.urls[service], self.draft.ca_bundle
        snapshot = Draft(urls={service: url}, ca_bundle=ca)
        self.inflight.add(service)
        self.query_one(f"#login-button-{service}", Button).disabled = True
        self.say(f"who-{service}", f"Checking the token with {LABELS[service]}...", "busy")
        self.setup.background(
            f"login-{service}",
            lambda: self.setup.services.login(snapshot, service, secret),
            lambda who, error: self._logged_in(service, url, ca, who, error),
        )

    def _logged_in(
        self,
        service: str,
        url: str,
        ca: str | None,
        who: str | None,
        error: YojoError | None,
    ) -> None:
        self.inflight.discard(service)
        self.query_one(f"#login-button-{service}", Button).disabled = False
        if self.draft.urls.get(service) != url or self.draft.ca_bundle != ca:
            if who is not None:
                if service not in self.draft.stored:
                    self.draft.stored.append(service)
                self.say(
                    f"who-{service}",
                    "Login saved for the previous address. "
                    "Check and log in to the current address.",
                    "warn",
                )
            return
        if who is None:
            assert error is not None
            self.say(f"who-{service}", f"Nothing was stored. {_error_text(error)}", "error")
            return
        self.draft.logins[service] = who + " Saved to your keyring."
        if service not in self.draft.stored:
            self.draft.stored.append(service)
        self.say(f"who-{service}", self.draft.logins[service], "ok")

    def blocker(self) -> str | None:
        if self.inflight:
            names = ", ".join(LABELS[s] for s in SERVICES if s in self.inflight)
            return f"Wait for the {names} login result before continuing."
        missing = [LABELS[s] for s in self.draft.urls if s not in self.draft.logins]
        if missing:
            names = ", ".join(missing)
            return f"Log in to {names} first, or go Back and clear its address to skip it."
        return None


class ChooseStep(StepView):
    number = 3

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.all_projects: list[dict[str, str]] = []
        self.all_repos: list[dict[str, Any]] = []
        self.next_repo: int | None = None
        self.loaded_for: tuple[tuple[str | None, str | None], ...] | None = None
        self.project_request: object | None = None
        self.repo_request: object | None = None

    def compose(self) -> ComposeResult:
        yield self.lead(
            "Pick the projects and repositories you work in. Type to search; Space selects."
        )
        with Vertical(id="jira-choice", classes="card"):
            yield Static("Jira projects", classes="card-title", markup=False)
            yield Input(placeholder="search projects by key or name", id="project-search")
            yield SelectionList[str](id="project-list")
            yield Static("", id="project-status", classes="status", markup=False)
        with Vertical(id="bitbucket-choice", classes="card"):
            yield Static("Bitbucket repositories", classes="card-title", markup=False)
            yield Static(
                "yojo.yaml has no repository list yet, so these are not saved. yojo uses your "
                "first pick in the try step; pass --repo PROJECT/slug to Bitbucket commands.",
                classes="hint",
                markup=False,
            )
            yield Input(placeholder="search loaded repositories", id="repo-search")
            yield SelectionList[str](id="repo-list")
            with Horizontal(classes="row"):
                yield Button("Load more", id="more-repos", disabled=True)
                yield Static("", id="repo-status", classes="status", markup=False)
        yield Static(
            "Confluence needs no choices here.",
            id="nothing-to-choose",
            classes="hint",
            markup=False,
        )

    def enter(self) -> None:
        has_jira = "jira" in self.draft.urls
        has_bitbucket = "bitbucket" in self.draft.urls
        self.query_one("#jira-choice").display = has_jira
        self.query_one("#bitbucket-choice").display = has_bitbucket
        self.query_one("#nothing-to-choose").display = not (has_jira or has_bitbucket)
        key = (self.draft.connection("jira"), self.draft.connection("bitbucket"))
        if key == self.loaded_for:
            return
        self.loaded_for = key
        self.project_request = self.repo_request = None
        self.all_projects, self.all_repos, self.next_repo = [], [], None
        self.query_one("#project-list", SelectionList).clear_options()
        self.query_one("#repo-list", SelectionList).clear_options()
        if has_jira:
            request = self.project_request = object()
            snapshot = copy.deepcopy(self.draft)
            self.say("project-status", "Loading projects...", "busy")
            self.setup.background(
                "projects",
                lambda: self.setup.services.projects(snapshot),
                lambda rows, error: (
                    self._projects(rows, error)
                    if request is self.project_request
                    and key == (self.draft.connection("jira"), self.draft.connection("bitbucket"))
                    else None
                ),
            )
            self.query_one("#project-search", Input).focus()
        if has_bitbucket:
            self.next_repo = 0
            self._load_repos()

    def _projects(self, rows: list[dict[str, str]] | None, error: YojoError | None) -> None:
        if rows is None:
            assert error is not None
            self.loaded_for = None
            self.say("project-status", f"Could not load projects. {_error_text(error)}", "error")
            return
        self.all_projects = rows
        known = {r["key"] for r in rows}
        unseen = [k for k in self.draft.projects if k not in known]
        if unseen:
            self.say(
                "project-status",
                f"Not visible to you now: {', '.join(unseen)}. They stay selected; the review "
                "step checks them again.",
                "warn",
            )
        self._filter_projects()

    def _filter_projects(self) -> None:
        query = self.query_one("#project-search", Input).value.strip().casefold()
        matches = [
            r
            for r in self.all_projects
            if query in r["key"].casefold() or query in r["name"].casefold()
        ]
        widget = self.query_one("#project-list", SelectionList)
        widget.clear_options()
        widget.add_options(
            Selection(plain(f"{r['key']}  {r['name']}"), r["key"], r["key"] in self.draft.projects)
            for r in matches[:MAX_SHOWN]
        )
        shown = min(len(matches), MAX_SHOWN)
        text = f"{self._chosen(self.draft.projects)} Showing {shown} of {len(matches)} matches"
        text += f" ({len(self.all_projects)} projects in Jira)."
        if len(matches) > MAX_SHOWN:
            text += " Type more to narrow the list."
        if not self.all_projects:
            text = "Jira shows you no projects. Ask a Jira admin for access, or press Next."
        self.say("project-status", text, "info")

    def _load_repos(self) -> None:
        start = self.next_repo or 0
        request = self.repo_request = object()
        snapshot = copy.deepcopy(self.draft)
        key = self.draft.connection("bitbucket")
        self.say("repo-status", "Loading repositories...", "busy")
        self.query_one("#more-repos", Button).disabled = True
        self.setup.background(
            "repos",
            lambda: self.setup.services.repos(snapshot, start),
            lambda page, error: (
                self._repos(page, error)
                if request is self.repo_request and key == self.draft.connection("bitbucket")
                else None
            ),
        )

    def _repos(self, page: dict[str, Any] | None, error: YojoError | None) -> None:
        if page is None:
            assert error is not None
            self.loaded_for = None
            self.say("repo-status", f"Could not load repositories. {_error_text(error)}", "error")
            return
        self.all_repos += page["repos"]
        self.next_repo = page["next_start"]
        self.query_one("#more-repos", Button).disabled = self.next_repo is None
        self._filter_repos()

    def _filter_repos(self) -> None:
        query = self.query_one("#repo-search", Input).value.strip().casefold()
        matches = [
            r
            for r in self.all_repos
            if query in r["repo"].casefold() or query in str(r["name"]).casefold()
        ]
        widget = self.query_one("#repo-list", SelectionList)
        widget.clear_options()
        widget.add_options(
            Selection(plain(f"{r['repo']}  {r['name']}"), r["repo"], r["repo"] in self.draft.repos)
            for r in matches[:MAX_SHOWN]
        )
        more = " More are available: press Load more." if self.next_repo is not None else ""
        text = f"{self._chosen(self.draft.repos)} {len(self.all_repos)} loaded.{more}"
        if not self.all_repos:
            text = "Bitbucket shows you no repositories. Press Next to continue without one."
        self.say("repo-status", text, "info")

    @on(Input.Changed, "#project-search")
    def _search_projects(self) -> None:
        self._filter_projects()

    @on(Input.Changed, "#repo-search")
    def _search_repos(self) -> None:
        self._filter_repos()

    @on(Input.Submitted, "#project-search")
    def _to_projects(self) -> None:
        widget = self.query_one("#project-list", SelectionList)
        if widget.option_count:
            widget.highlighted = 0
        widget.focus()

    @on(Input.Submitted, "#repo-search")
    def _to_repos(self) -> None:
        widget = self.query_one("#repo-list", SelectionList)
        if widget.option_count:
            widget.highlighted = 0
        widget.focus()

    @on(Button.Pressed, "#more-repos")
    def _more(self) -> None:
        self._load_repos()

    @on(SelectionList.SelectionToggled, "#project-list")
    def _toggle_project(self, event: SelectionList.SelectionToggled[str]) -> None:
        self._toggle(self.draft.projects, str(event.selection.value))
        self._filter_projects_status()

    @on(SelectionList.SelectionToggled, "#repo-list")
    def _toggle_repo(self, event: SelectionList.SelectionToggled[str]) -> None:
        self._toggle(self.draft.repos, str(event.selection.value))
        self.say("repo-status", self._chosen(self.draft.repos), "info")

    def _filter_projects_status(self) -> None:
        self.say("project-status", self._chosen(self.draft.projects), "info")

    @staticmethod
    def _chosen(values: list[str]) -> str:
        # Named in words too, so the choice is clear without color (NO_COLOR, monochrome).
        if not values:
            return "None selected."
        shown = ", ".join(values[:8]) + (f" and {len(values) - 8} more" if len(values) > 8 else "")
        return f"Selected: {shown}."

    def _toggle(self, chosen: list[str], value: str) -> None:
        if value in chosen:
            chosen.remove(value)
        else:
            chosen.append(value)
        self.draft.touch()


class RulesStep(StepView):
    number = 4

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields: list[FieldChoice] = []
        self.meta: dict[str, ProjectMeta] = {}
        self.loaded_for: tuple[str | None, str | None] | None = None
        self.field_request: object | None = None
        self.meta_request: object | None = None

    def compose(self) -> ComposeResult:
        yield self.lead(
            "Optional. Add only rules your team already follows; yojo never adds any on its own. "
            "Names come from your Jira. Press Next to skip."
        )
        yield Static("", id="rules-status", classes="status", markup=False)
        yield Button("Retry loading fields", id="retry-fields", classes="retry")
        with Vertical(id="rules-editor"):
            with Horizontal(classes="row"):
                yield Label("Project", classes="field-label")
                yield Select[str]([], id="rule-project", prompt="choose a project")
            with TabbedContent(id="rule-tabs"):
                with TabPane("Friendly names", id="tab-alias"):
                    yield Static(
                        "A short name you can use in --set instead of the Jira field name.",
                        classes="hint",
                        markup=False,
                    )
                    yield Select[str]([], id="alias-field", prompt="Jira field")
                    yield Input(placeholder="short name, e.g. points", id="alias-name")
                    yield Button("Add friendly name", id="add-alias")
                with TabPane("Required on create", id="tab-create"):
                    yield Select[str]([], id="create-type", prompt="issue type")
                    yield Select[str]([], id="create-field", prompt="field that must have a value")
                    yield Button("Add requirement", id="add-create")
                with TabPane("Transition rules", id="tab-transition"):
                    with Horizontal(classes="row"):
                        yield Select[str]([], id="move-from", prompt="from status")
                        yield Select[str]([], id="move-to", prompt="to status")
                    yield Select[str]([], id="move-field", prompt="field that must have a value")
                    yield Input(
                        placeholder="attachment name pattern, e.g. {key}_Evidence_*",
                        id="move-pattern",
                    )
                    with Horizontal(classes="row"):
                        yield Select[str]([], id="move-set-field", prompt="field to set (optional)")
                        yield Input(placeholder="value to set", id="move-set-value")
                    yield Checkbox(
                        "If that field is not on the transition screen, set it with a separate "
                        "edit first (two writes, not atomic)",
                        id="move-pre-edit",
                    )
                    yield Button("Add transition rule", id="add-transition")
        with Vertical(classes="card", id="rules-summary"):
            yield Static("Your rules", classes="card-title", markup=False)
            yield ListView(id="rule-list")
        yield Button("Remove selected rule", id="remove-rule")

    def enter(self) -> None:
        editor = self.query_one("#rules-editor")
        if "jira" not in self.draft.urls or not self.draft.projects:
            editor.display = False
            self.say(
                "rules-status",
                "Rules need a Jira project. Choose one in step 3, or press Next to skip.",
                "info",
            )
        else:
            editor.display = True
            picker = self.query_one("#rule-project", Select)
            current = picker.value
            picker.set_options((k, k) for k in self.draft.projects)
            picker.value = current if current in self.draft.projects else self.draft.projects[0]
            if self.loaded_for != self.draft.connection("jira"):
                self.fields, self.meta = [], {}
                self.loaded_for = None
                for target in ("#alias-field", "#create-field", "#move-field", "#move-set-field"):
                    self.query_one(target, Select).set_options([])
                self._load_fields()
            else:
                self.say("rules-status", "", "info")
        dropped = [
            r
            for r in self.draft.rules
            if r.kind != "alias" and r.project not in self.draft.projects
        ]
        if dropped:
            self.setup.notify(
                f"{len(dropped)} rule(s) belong to projects you did not choose; they are left out.",
                severity="warning",
            )
        self._list_rules()

    @on(Button.Pressed, "#retry-fields")
    def _load_fields(self) -> None:
        self.query_one("#retry-fields", Button).display = False
        request = self.field_request = object()
        key = self.draft.connection("jira")
        snapshot = copy.deepcopy(self.draft)
        self.say("rules-status", "Loading Jira fields...", "busy")
        self.setup.background(
            "fields",
            lambda: self.setup.services.fields(snapshot),
            lambda fields, error: (
                self._fields(fields, error)
                if request is self.field_request and key == self.draft.connection("jira")
                else None
            ),
        )

    def _fields(self, fields: list[FieldChoice] | None, error: YojoError | None) -> None:
        if fields is None:
            assert error is not None
            self.query_one("#retry-fields", Button).display = True
            self.say("rules-status", f"✗ Could not load Jira fields. {_error_text(error)}", "error")
            return
        self.fields = fields
        self.loaded_for = self.draft.connection("jira")
        options = [(plain(f"{f.name}  ({f.id})"), f.ref) for f in fields]
        for target in ("#alias-field", "#create-field", "#move-field", "#move-set-field"):
            self.query_one(target, Select).set_options(options)
        self.say("rules-status", f"{len(fields)} Jira fields loaded.", "info")

    @on(Select.Changed, "#rule-project")
    def _project(self, event: Select.Changed) -> None:
        if event.value is Select.NULL:
            return
        key = str(event.value)
        if key not in self.draft.projects:
            return
        if key in self.meta:
            self._apply_meta(self.meta[key])
            return
        request = self.meta_request = object()
        connection = self.draft.connection("jira")
        snapshot = copy.deepcopy(self.draft)
        self.say("rules-status", f"Loading issue types and statuses for {key}...", "busy")
        self.setup.background(
            f"meta-{key}",
            lambda: self.setup.services.project_meta(snapshot, key),
            lambda meta, error: (
                self._meta(meta, error)
                if request is self.meta_request
                and connection == self.draft.connection("jira")
                and key in self.draft.projects
                and self._project_key() == key
                else None
            ),
        )

    def _meta(self, meta: ProjectMeta | None, error: YojoError | None) -> None:
        if meta is None:
            assert error is not None
            self.say(
                "rules-status", f"Could not load project details. {_error_text(error)}", "error"
            )
            return
        self.meta[meta.key] = meta
        self._apply_meta(meta)
        self.say(
            "rules-status",
            f"{meta.key}: {len(meta.issue_types)} issue types, {len(meta.statuses)} statuses.",
            "info",
        )

    def _apply_meta(self, meta: ProjectMeta) -> None:
        self.query_one("#create-type", Select).set_options((plain(t), t) for t in meta.issue_types)
        statuses = [(plain(s), s) for s in meta.statuses]
        self.query_one("#move-from", Select).set_options(statuses)
        self.query_one("#move-to", Select).set_options(statuses)

    def _value(self, selector: str) -> str | None:
        value = self.query_one(selector, Select).value
        return None if value is Select.NULL else str(value)

    def _project_key(self) -> str | None:
        key = self._value("#rule-project")
        if key is None:
            self.setup.notify("Choose a project first.", severity="warning")
        return key

    def _ref(self, value: str) -> dict[str, str]:
        choice = next((f for f in self.fields if f.ref == value), None)
        if choice is not None and choice.ref == choice.id:
            return {"id": choice.id}
        return {"jira_name": value}

    @on(Button.Pressed, "#add-alias")
    def add_alias(self) -> None:
        field_ref = self._value("#alias-field")
        name = self.query_one("#alias-name", Input).value.strip()
        if field_ref is None or not FRIENDLY_RE.fullmatch(name):
            self.setup.notify(
                "Choose a field and a short name in lowercase letters, digits, and _ "
                "(starting with a letter).",
                severity="warning",
            )
            return
        self._add(Rule("alias", None, name, self._ref(field_ref)), replace_same=True)
        self.query_one("#alias-name", Input).value = ""

    @on(Button.Pressed, "#add-create")
    def add_create(self) -> None:
        key = self._project_key()
        issue_type = self._value("#create-type")
        field_ref = self._value("#create-field")
        if key is None or issue_type is None or field_ref is None:
            if key is not None:
                self.setup.notify("Choose an issue type and a field.", severity="warning")
            return
        current = next(
            (
                r
                for r in self.draft.rules
                if r.kind == "create"
                and r.project == key
                and r.name.casefold() == issue_type.casefold()
            ),
            None,
        )
        required = list(current.value["required"]) if current else []
        if field_ref not in required:
            required.append(field_ref)
        self._add(Rule("create", key, issue_type, {"required": required}), replace_same=True)

    @on(Button.Pressed, "#add-transition")
    def add_transition(self) -> None:
        key = self._project_key()
        source, target = self._value("#move-from"), self._value("#move-to")
        if key is None:
            return
        if source is None or target is None:
            self.setup.notify("Choose a from status and a to status.", severity="warning")
            return
        require: list[dict[str, str]] = []
        needed = self._value("#move-field")
        if needed is not None:
            require.append({"kind": "field_set", "field": needed})
        pattern = self.query_one("#move-pattern", Input).value.strip()
        if pattern:
            require.append({"kind": "attachment_filename", "pattern": pattern})
        rule: dict[str, Any] = {"from": [source], "to": target}
        set_field = self._value("#move-set-field")
        set_value = self.query_one("#move-set-value", Input).value.strip()
        if (set_field is None) != (not set_value):
            self.setup.notify(
                "Give both a field to set and its value, or neither.", severity="warning"
            )
            return
        if set_field is not None:
            rule["auto_set"] = {set_field: set_value}
            rule["pre_edit"] = self.query_one("#move-pre-edit", Checkbox).value
        if require:
            rule["require"] = require
        if not require and set_field is None:
            self.setup.notify(
                "Add a field that must have a value, an attachment pattern, or a field to set.",
                severity="warning",
            )
            return
        taken = {r.name for r in self.draft.rules if r.kind == "transition" and r.project == key}
        self._add(Rule("transition", key, rule_name(target, taken), rule))
        for selector in ("#move-pattern", "#move-set-value"):
            self.query_one(selector, Input).value = ""

    def _add(self, rule: Rule, replace_same: bool = False) -> None:
        if replace_same:
            self.draft.rules = [
                r
                for r in self.draft.rules
                if not (
                    r.kind == rule.kind
                    and r.project == rule.project
                    and r.name.casefold() == rule.name.casefold()
                )
            ]
        self.draft.rules.append(rule)
        self.draft.touch()
        self._list_rules()
        self.setup.notify(clean_lines(rule.describe(), 1000), title="Rule added", markup=False)

    def _list_rules(self) -> None:
        view = self.query_one("#rule-list", ListView)
        view.clear()
        shown = [
            r for r in self.draft.rules if r.kind == "alias" or r.project in self.draft.projects
        ]
        if not shown:
            view.append(
                ListItem(
                    Static("No rules. That is fine: Jira's own checks still apply.", markup=False),
                    disabled=True,
                )
            )
        for rule in shown:
            origin = " (from your yojo.yaml)" if rule.origin == "file" else ""
            item = ListItem(Static(plain(rule.describe() + origin, 1000)))
            item.rule = rule  # type: ignore[attr-defined]
            view.append(item)

    @on(Button.Pressed, "#remove-rule")
    def remove_rule(self) -> None:
        item = self.query_one("#rule-list", ListView).highlighted_child
        rule = getattr(item, "rule", None)
        if rule is None:
            self.setup.notify("Highlight a rule in the list first.", severity="warning")
            return
        self.draft.rules.remove(rule)
        self.draft.touch()
        self._list_rules()


class ReviewStep(StepView):
    number = 5

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.candidate: init.Candidate | None = None
        self.reviewed_revision: int | None = None
        self.stale = False
        self.request: object | None = None

    def compose(self) -> ComposeResult:
        yield self.lead("Review the exact yojo.yaml and its changes against the current file.")
        with Horizontal(classes="row"):
            yield Label("Save to", classes="field-label")
            yield Input(value=CONFIG_FILENAME, id="output")
        yield Static("", id="review-status", classes="status", markup=False)
        with TabbedContent(id="review-tabs"):
            with TabPane("Changes", id="tab-diff"), VerticalScroll(id="diff-panel"):
                yield Static("", id="diff", markup=False)
            with TabPane("Full file", id="tab-yaml"):
                yield Static("", id="yaml", markup=False)
        yield Static("Nothing is written until you press Save.", id="save-note", markup=False)
        yield Static("", id="review-notes", classes="hint", markup=False)
        with Horizontal(classes="row"):
            yield Button("Refresh preview", id="preview")
            yield Button("Save yojo.yaml", id="save", variant="primary", disabled=True)

    def enter(self) -> None:
        if self.reviewed_revision != self.draft.revision or self.candidate is None:
            self.build()

    @on(Input.Changed, "#output")
    def _output(self, event: Input.Changed) -> None:
        value = event.value.strip() or CONFIG_FILENAME
        if value != self.draft.output:
            self.draft.output = value
            self.draft.touch()
            self._invalidate("The destination changed. Press Refresh preview.")

    def _invalidate(self, why: str) -> None:
        self.candidate = None
        self.query_one("#save", Button).disabled = True
        self.request = object()
        self.say("review-status", why, "warn")

    @on(Input.Submitted, "#output")
    @on(Button.Pressed, "#preview")
    def build(self) -> None:
        self.query_one("#save", Button).disabled = True
        self.request = object()
        request = self.request
        self.say(
            "review-status", "Building the preview and checking it against live data...", "busy"
        )
        revision = self.draft.revision
        snapshot = copy.deepcopy(self.draft)
        self.setup.background(
            "candidate",
            lambda: self.setup.services.candidate(snapshot),
            lambda result, error: (
                self._built(revision, result, error) if request is self.request else None
            ),
        )

    def _built(
        self, revision: int, candidate: init.Candidate | None, error: YojoError | None
    ) -> None:
        if revision != self.draft.revision:
            self._invalidate("Your choices changed while the preview was built. Refresh it.")
            return
        if candidate is None:
            assert error is not None
            self.candidate = None
            self.say("review-status", f"Cannot save yet. {_error_text(error)}", "error")
            return
        self.candidate, self.reviewed_revision, self.stale = candidate, revision, False
        where = show_path(candidate.dest, self.setup.services.cwd)
        diff = unified_diff(candidate.current, candidate.text, where)
        text = Text()
        for number, line in enumerate(diff or ["No changes: the file already matches."], 1):
            style = "green" if line.startswith("+") else "red" if line.startswith("-") else ""
            text.append(f"{number:>3}  ", style="dim")
            text.append(sanitize_text(line, 1000) + "\n", style=style)
        self.query_one("#diff", Static).update(text)
        self.query_one("#yaml", Static).update(Text(candidate.text))
        if candidate.current is None:
            status = f"{where} does not exist yet. Save creates it."
        elif candidate.current == candidate.data:
            status = f"{where} already matches. Nothing to save."
        else:
            changed = sum(1 for line in diff if line[:1] in "+-" and line[:3] not in ("+++", "---"))
            status = f"Save replaces {where} ({changed} changed lines). Review the changes first."
        self.say("review-status", status, "ok")
        notes = list(self.draft.notes)
        if self.draft.repos:
            notes.append(
                "Bitbucket repositories are checked but not saved (no repository list "
                "in yojo.yaml version 1)."
            )
        if self.draft.policy() is None:
            notes.append("No team rules: yojo uses Jira's own required fields and checks.")
        self.query_one("#review-notes", Static).update(plain("\n".join(notes), 2000))
        self.query_one("#save", Button).disabled = candidate.current == candidate.data

    @on(Button.Pressed, "#save")
    def save(self) -> None:
        candidate = self.candidate
        if candidate is None or self.reviewed_revision != self.draft.revision:
            self._invalidate("Your choices changed after this preview. Press Refresh preview.")
            return
        self.query_one("#save", Button).disabled = True
        self.say("review-status", "Saving...", "busy")
        revision = self.draft.revision
        self.setup.background(
            "save",
            lambda: self.setup.services.save(candidate),
            lambda action, error: self._saved(candidate, revision, action, error),
            writes=True,
        )

    def _saved(
        self, candidate: init.Candidate, revision: int, action: str | None, error: YojoError | None
    ) -> None:
        if action is None:
            assert error is not None
            self.candidate, self.stale = None, True
            self.say(
                "review-status",
                f"Not saved; nothing was written. {_error_text(error)}\n"
                "Press Refresh preview to review the current state.",
                "error",
            )
            return
        self.draft.saved = (str(candidate.dest), action)
        self.draft.saved_data = candidate.data
        self.draft.saved_revision = revision
        self.draft.try_revision = None
        if revision != self.draft.revision:
            self._invalidate("Saved the earlier preview. Your choices changed; Refresh preview.")
        elif self.setup.step == self.number:
            self.setup.query_one("#next", Button).label = "Next"
        where = show_path(candidate.dest, self.setup.services.cwd)
        verb = {"created": "Created", "replaced": "Saved", "unchanged": "Already up to date:"}
        if revision == self.draft.revision:
            self.say("review-status", f"{verb[action]} {where}. Next, connect your agent.", "ok")
        self.setup.notify(f"{verb[action]} {where}", title="yojo.yaml", markup=False)

    def blocker(self) -> str | None:
        if not self.setup.saved_snapshot_current():
            return "Review the current preview and press Save before continuing."
        return None


class AgentStep(StepView):
    number = 6

    def compose(self) -> ComposeResult:
        yield self.lead(
            "Optional. Write yojo's instructions where your AI tools look for them, so an agent "
            "can use yojo in one call. Only yojo's marked section is written; your text is kept."
        )
        yield SelectionList[str](
            *(
                Selection(plain(f"{label}  [{name}]"), name, False)
                for name, label in AGENT_LABELS.items()
            ),
            id="agent-targets",
        )
        yield Button("Install instructions", id="install", variant="primary")
        yield Static("", id="agent-status", classes="status", markup=False)
        yield Static("", id="agent-command", classes="hint", markup=False)

    def enter(self) -> None:
        self.query_one("#agent-targets", SelectionList).focus()

    def _targets(self) -> list[str]:
        chosen = set(self.query_one("#agent-targets", SelectionList).selected)
        return [name for name in AGENT_LABELS if name in chosen]

    @on(Button.Pressed, "#install")
    def install(self) -> None:
        targets = self._targets()
        if not targets:
            self.say(
                "agent-status", "Select at least one tool (Space), or press Next to skip.", "warn"
            )
            return
        argv = [retry.PROGRAM, "--json", "agent", "install"]
        for name in targets:
            argv += ["--target", name]
        started = self.setup.background(
            "agents",
            lambda: self.setup.services.install_agents(targets),
            self._installed,
            writes=True,
        )
        if not started:
            return
        self.query_one("#agent-command", Static).update(plain(f"Same as: {retry.posix(argv)}", 500))
        self.say("agent-status", "Writing instructions...", "busy")

    def _installed(self, result: dict[str, Any] | None, error: YojoError | None) -> None:
        if result is None:
            assert error is not None
            written = error.details.get("written") or []
            lines = [f"Not everything was written. {_error_text(error)}"]
            lines += [f"Written: {w.get('path')} ({w.get('action')})" for w in written]
            self.setup.record_agents(written)
            self.say("agent-status", "\n".join(lines), "error")
            return
        rows = result["files"]
        self.setup.record_agents(rows)
        lines = [f"{r['path']}: {r['action']}" for r in rows]
        self.say("agent-status", "Done.\n" + "\n".join(lines), "ok")


class TryStep(StepView):
    number = 7

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.request: object | None = None

    def compose(self) -> ComposeResult:
        yield self.lead(
            "Run one real, read-only command to see yojo work. Nothing is changed on the server."
        )
        yield Static("", id="finish-message", classes="card", markup=False)
        yield Static("", id="try-command", classes="command", markup=False)
        yield Button("Run it", id="run-try", variant="primary")
        yield Static("", id="try-status", classes="status", markup=False)
        yield DataTable(id="try-table", show_cursor=False)
        yield Static("", id="try-next", classes="hint", markup=False)

    def enter(self) -> None:
        argv = self.setup.services.try_argv(self.draft)
        self.query_one("#try-command", Static).update(plain(retry.posix(argv), 1000))
        self.query_one("#try-table", DataTable).display = False
        self.query_one("#try-next", Static).update(plain("\n".join(self.setup.next_steps()), 2000))
        self.query_one("#finish-message", Static).update(
            plain("yojo.yaml is saved. One last sounding: run the read-only check.")
        )
        self.query_one("#run-try", Button).focus()

    @on(Button.Pressed, "#run-try")
    def run_try(self) -> None:
        if not self.setup.saved_snapshot_current():
            self.say("try-status", "Save the current yojo.yaml before trying it.", "warn")
            return
        argv = self.setup.services.try_argv(self.draft)
        revision = self.draft.revision
        request = self.request = object()
        self.draft.try_revision = None
        self.say("try-status", "Running...", "busy")
        self.setup.background(
            "try",
            lambda: self.setup.services.run_read(argv),
            lambda result, error: (
                self._ran(result, error)
                if request is self.request and revision == self.draft.revision
                else None
            ),
        )

    def _ran(self, result: tuple[dict[str, Any], int] | None, error: YojoError | None) -> None:
        if result is None:
            assert error is not None
            self.say("try-status", _error_text(error), "error")
            return
        envelope, code = result
        if not envelope.get("ok"):
            self.say(
                "try-status",
                f"✗ Read failed (exit {code}). Check the connection and Retry the read-only check.",
                "error",
            )
            return
        if code != 0:
            self.say("try-status", f"✗ Read returned exit {code}. Retry it.", "error")
            return
        if not self.setup.saved_snapshot_current():
            self.say("try-status", "The saved file changed. Review and Save it again.", "error")
            return
        self.draft.try_revision = self.draft.revision
        data = envelope.get("data") or {}
        rows: list[dict[str, Any]] = []
        columns: list[str] = []
        for key, cols in (
            ("issues", ["key", "status", "summary"]),
            ("pull_requests", ["id", "state", "title"]),
            ("results", ["id", "space_key", "title"]),
            ("repos", ["repo", "name"]),
        ):
            if isinstance(data.get(key), list):
                rows, columns = data[key][: int(TRY_LIMIT)], cols
                break
        table = self.query_one("#try-table", DataTable)
        table.clear(columns=True)
        table.add_columns(*columns)
        for row in rows:
            table.add_row(
                *(
                    Text(sanitize_text(row.get(c) if row.get(c) is not None else "", 70))
                    for c in columns
                )
            )
        table.display = bool(rows)
        self.say(
            "try-status",
            f"It works: {len(rows)} result(s), exit 0."
            if rows
            else "It works (exit 0), and there is nothing to show yet.",
            "ok",
        )
        where = self.draft.saved[0] if self.draft.saved else "yojo.yaml"
        saved_line = f"✓ {show_path(Path(where), self.setup.services.cwd)} saved"
        self.query_one("#finish-message", Static).update(
            plain(
                f"{saved_line}; read-only Try passed.\n"
                f"Next: {self.setup.next_steps()[0]}\n"
                "Fair winds for the next command."
            )
        )

    def blocker(self) -> str | None:
        if not self.setup.saved_snapshot_current():
            return "Save the current yojo.yaml before finishing."
        if self.draft.try_revision != self.draft.revision:
            return "Run the read-only command successfully before finishing."
        return None


class LeaveScreen(ModalScreen[bool]):
    BINDINGS: ClassVar = [Binding("escape", "stay", "Keep going")]

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="leave"):
            yield Static("Leave setup?", classes="card-title", markup=False)
            yield Static(self.message, markup=False)
            with Horizontal(classes="row"):
                yield Button("Keep going", id="stay", variant="primary")
                yield Button("Leave setup", id="leave-now", variant="error")

    def on_mount(self) -> None:
        self.query_one("#stay", Button).focus()

    @on(Button.Pressed, "#stay")
    def action_stay(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#leave-now")
    def _leave(self) -> None:
        self.dismiss(True)


# --------------------------------------------------------------------------- app


class SetupApp(App[dict[str, Any]]):
    CSS_PATH = "setup.tcss"
    TITLE = "yojo setup"
    ENABLE_COMMAND_PALETTE = False
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (80, "-wide")]  # noqa: RUF012 - Textual's shape
    BINDINGS: ClassVar = [
        Binding("ctrl+n", "next_step", "Next", priority=True),
        Binding("ctrl+b", "previous_step", "Back", priority=True),
        Binding("ctrl+t", "theme", "Light/dark", priority=True),
        Binding("escape", "leave", "Leave"),
        Binding("ctrl+q", "leave", "Leave", show=False, priority=True),
        Binding("ctrl+c", "leave", "Leave", show=False, priority=True),
    ]

    def __init__(self, services: Services, draft: Draft | None = None, theme: str | None = None):
        super().__init__()
        self.register_theme(Theme(name="yojo-dark", primary="#33b6a7", dark=True))
        self.register_theme(Theme(name="yojo-light", primary="#087e73", dark=False))
        self.services = services
        self.draft = draft or Draft()
        self.step = 1
        self.result_status = "cancelled"
        self.writing: set[str] = set()
        self.theme = "yojo-light" if theme in ("textual-light", "yojo-light") else "yojo-dark"

    def compose(self) -> ComposeResult:
        with Vertical(id="top"), Horizontal(id="progress"):
            for n, name in enumerate(CRUMBS, 1):
                yield Static(f"{n} {name}", id=f"crumb-{n}", classes="crumb", markup=False)
            yield Static("", id="step-title", markup=False)
        with ContentSwitcher(initial="step-1", id="steps"):
            yield ConnectStep(id="step-1")
            yield LoginStep(id="step-2")
            yield ChooseStep(id="step-3")
            yield RulesStep(id="step-4")
            yield ReviewStep(id="step-5")
            yield AgentStep(id="step-6")
            yield TryStep(id="step-7")
        with Horizontal(id="nav"):
            yield Static("", id="nav-hint", markup=False)
            yield Button("Back", id="back")
            yield Button("Next", id="next", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self._show(1)

    # Navigation ---------------------------------------------------------------------------

    def current(self) -> StepView:
        view = self.query_one(f"#step-{self.step}", StepView)
        return view

    def _show(self, step: int) -> None:
        self.step = step
        self.query_one("#steps", ContentSwitcher).current = f"step-{step}"
        compact = self.screen.size.width < 100
        for n in range(1, len(STEPS) + 1):
            crumb = self.query_one(f"#crumb-{n}", Static)
            number = "✓" if n < step else str(n)
            crumb.update(plain(number if compact else f"{number} {CRUMBS[n - 1]}"))
            crumb.set_classes(
                "crumb" + (" -current" if n == step else " -done" if n < step else "")
            )
        self.query_one("#step-title", Static).update(
            plain(f"Step {step} of {len(STEPS)}" + (f" · {STEPS[step - 1]}" if compact else ""))
        )
        self.query_one("#back", Button).display = step > 1
        self.query_one("#next", Button).label = (
            "Finish"
            if step == len(STEPS)
            else "Save"
            if step == 5 and self.current().blocker() is not None
            else "Next"
        )
        self.query_one("#next", Button).variant = "primary"
        self.query_one("#nav-hint", Static).update("")
        self.current().enter()
        if step == 1:
            self.query_one("#url-jira", Input).focus()

    def write_pending(self) -> str | None:
        """Why setup cannot move on: a local file write has not reported its result yet."""
        if "save" in self.writing:
            return "yojo.yaml is still being saved. Wait for the result before moving on."
        if "agents" in self.writing:
            return "Agent instructions are still being written. Wait for the result first."
        return None

    def saved_snapshot_current(self) -> bool:
        saved = self.draft.saved
        if (
            saved is None
            or self.draft.saved_data is None
            or self.draft.saved_revision != self.draft.revision
        ):
            return False
        try:
            return files.read_current(Path(saved[0])) == self.draft.saved_data
        except (YojoError, OSError):
            return False

    def discard_discovery(self, service: str) -> None:
        """Clear visible choices as soon as their connection changes, including while off-step."""
        choices = self.query_one("#step-3", ChooseStep)
        choices.loaded_for = None
        if service == "jira":
            choices.project_request = None
            choices.all_projects = []
            choices.query_one("#project-list", SelectionList).clear_options()
            rules = self.query_one("#step-4", RulesStep)
            rules.field_request = rules.meta_request = None
            rules.loaded_for = None
            rules.fields, rules.meta = [], {}
            for target in ("#alias-field", "#create-field", "#move-field", "#move-set-field"):
                rules.query_one(target, Select).set_options([])
        elif service == "bitbucket":
            choices.repo_request = None
            choices.all_repos, choices.next_repo = [], None
            choices.query_one("#repo-list", SelectionList).clear_options()

    def _hold(self, reason: str) -> None:
        self.query_one("#nav-hint", Static).update(plain(reason))
        self.notify(reason, severity="warning", markup=False)

    def action_next_step(self) -> None:
        if (pending := self.write_pending()) is not None:
            self._hold(pending)
            return
        if self.step == 5 and (reason := self.current().blocker()) is not None:
            self.query_one("#step-5", ReviewStep).save()
            return
        if self.step == len(STEPS):
            reason = self.current().blocker()
            if reason is not None:
                self._hold(reason)
            else:
                self.finish("completed")
            return
        reason = self.current().blocker()
        if reason is not None:
            self._hold(reason)
            return
        self._show(self.step + 1)

    def action_previous_step(self) -> None:
        if (pending := self.write_pending()) is not None:
            self._hold(pending)
            return
        if self.step > 1:
            self._show(self.step - 1)

    @on(Button.Pressed, "#next")
    def _next(self) -> None:
        self.action_next_step()

    @on(Button.Pressed, "#back")
    def _back(self) -> None:
        self.action_previous_step()

    def action_theme(self) -> None:
        self.theme = "yojo-light" if self.current_theme.dark else "yojo-dark"

    def action_leave(self) -> None:
        if isinstance(self.screen, LeaveScreen):
            return
        login = self.query_one("#step-2", LoginStep)
        if login.inflight:
            self.notify(
                "A login is still being checked. Wait for its result before leaving setup.",
                severity="warning",
                markup=False,
            )
            return
        if (pending := self.write_pending()) is not None:
            self._hold(pending)
            return
        self.push_screen(LeaveScreen(self.leave_message()), self._left)

    def _left(self, leave: bool | None) -> None:
        if leave:
            self.finish("cancelled")

    def leave_message(self) -> str:
        lines = [
            f"yojo.yaml was saved ({self.draft.saved[1]})."
            if self.draft.saved
            else "yojo.yaml was not changed."
        ]
        if self.draft.stored:
            names = ", ".join(LABELS[s] for s in self.draft.stored)
            lines.append(
                f"Logins you stored stay in your OS keyring: {names}. "
                "Run `yojo login --service NAME` to replace one."
            )
        lines.append("Run `yojo setup` any time to pick up again.")
        return "\n".join(lines)

    # Results ------------------------------------------------------------------------------

    def record_agents(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            entry: dict[str, Any] = {k: row[k] for k in ("target", "path", "action") if k in row}
            self.draft.agent_files = [
                r for r in self.draft.agent_files if r.get("target") != entry.get("target")
            ] + [entry]

    def next_steps(self) -> list[str]:
        steps: list[str] = []
        if self.draft.saved is not None:
            path = Path(self.draft.saved[0])
            config = [] if path == self.services.cwd / CONFIG_FILENAME else ["--config", str(path)]
            steps.append(retry.posix([retry.PROGRAM, "--json", "config", "check", *config]))
        else:
            steps.append("yojo setup  (to save yojo.yaml later)")
        steps.append(retry.posix(self.services.try_argv(self.draft)))
        if not self.draft.agent_files:
            steps.append("yojo --json agent install --target agents")
        steps.append("yojo commands")
        return steps

    def summary(self) -> dict[str, Any]:
        config = None
        if self.draft.saved is not None:
            config = {"path": self.draft.saved[0], "action": self.draft.saved[1]}
        notes = []
        if self.result_status == "cancelled":
            notes = self.leave_message().splitlines()[1:]
        return {
            "status": self.result_status,
            "config": config,
            "logins_stored": list(self.draft.stored),
            "agent_files": list(self.draft.agent_files),
            "notes": notes,
            "next": self.next_steps() if self.draft.urls else ["yojo setup"],
        }

    def finish(self, status: str) -> None:
        if (pending := self.write_pending()) is not None:
            self._hold(pending)
            return
        if status == "completed" and (reason := self.query_one("#step-7", TryStep).blocker()):
            self._hold(reason)
            return
        self.result_status = status
        self.exit(self.summary())

    # Background work ----------------------------------------------------------------------

    def background(
        self,
        group: str,
        job: Callable[[], T],
        done: Callable[[T | None, YojoError | None], None],
        *,
        writes: bool = False,
    ) -> bool:
        """Run blocking network or file work off the UI thread, then report on it.

        A ``writes`` job changes local files: setup holds navigation and exit until its result
        is shown, and refuses to start a second one in the same group meanwhile.
        """
        if writes:
            if group in self.writing:
                return False
            self.writing.add(group)

        def report(result: T | None, error: YojoError | None) -> None:
            self.writing.discard(group)
            done(result, error)

        def runner() -> None:
            result: T | None = None
            error: YojoError | None = None
            try:
                result = job()
            except YojoError as exc:
                error = exc
            except Exception as exc:  # noqa: BLE001 - shown as a message, never a traceback
                error = YojoError(
                    "internal_error",
                    "yojo hit an unexpected internal error.",
                    "Try again; report the error_type to the yojo maintainers if it repeats.",
                    {"error_type": type(exc).__name__},
                )
            # The app may already be closing; then there is no one left to tell.
            with contextlib.suppress(RuntimeError):
                self.call_from_thread(report, result, error)

        self.run_worker(runner, group=group, exclusive=True, thread=True, exit_on_error=False)
        return True


def run_app(rt: Runtime) -> dict[str, Any]:
    services = Services(rt)
    draft = draft_from_file(services.existing())
    app = SetupApp(services, draft)
    result = app.run()
    return result if result is not None else app.summary()
