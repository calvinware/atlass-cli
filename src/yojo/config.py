"""Optional, strict, versioned ``yojo.yaml`` discovery, parsing, and validation.

``yojo`` works without a config file: service commands take ``--base-url`` and read live Jira
metadata. A ``yojo.yaml`` adds friendly field aliases and team rules on top, and may name only the
services a team uses (for example Bitbucket alone).
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

from yojo.contracts import PROJECT_KEY_PATTERN, YojoError

CONFIG_FILENAME = "yojo.yaml"
LEGACY_FILENAME = "atl.yaml"
MAX_CONFIG_BYTES = 256 * 1024
DEFAULT_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
SUPPORTED_VERSIONS = {1}
REQUIREMENT_KINDS = {"attachment_filename", "field_set"}
SERVICES = ("jira", "confluence", "bitbucket")

FRIENDLY_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
FIELD_ID_RE = re.compile(r"^(customfield_[0-9]{1,10}|[a-z][A-Za-z]{0,63})$")
PROJECT_KEY_RE = re.compile(PROJECT_KEY_PATTERN)
PATTERN_ALLOWED_RE = re.compile(r"^[A-Za-z0-9 ._\-*?\[\]!{}()+=,@#~]+$")
LEGACY_FIX = (
    "Rename {path} to {target} and review it for the yojo version 1 format: keep base_url, "
    "ca_bundle, max_attachment_bytes, and transitions; keep fields entries but drop each "
    "`encoding` (values are coerced from live Jira metadata); remove allowed_issue_types, "
    "story_type, feature_type, feature_create_policy, and create.story/create.feature (use "
    "create.<Issue Type>.required instead). See docs/configuration.md. yojo never migrates "
    "the file automatically."
)


@dataclass(frozen=True)
class FieldRef:
    """A configured alias for a Jira field, by its UI name or its raw ID."""

    friendly: str
    jira_name: str | None = None
    id: str | None = None

    @property
    def label(self) -> str:
        return self.jira_name or self.id or self.friendly


@dataclass(frozen=True)
class Requirement:
    kind: str
    pattern: str | None = None
    field: str | None = None

    def expand(self, issue_key: str) -> str:
        return (self.pattern or "").replace("{key}", issue_key)

    @property
    def label(self) -> str:
        return str(self.pattern if self.kind == "attachment_filename" else self.field)


@dataclass(frozen=True)
class TransitionConfig:
    name: str
    from_statuses: tuple[str, ...]
    to: str
    pre_edit: bool
    auto_set: dict[str, str | list[str]]
    require: tuple[Requirement, ...]


@dataclass(frozen=True)
class ProjectConfig:
    key: str
    fields: dict[str, FieldRef] = field(default_factory=dict)
    create_required: dict[str, tuple[str, ...]] = field(default_factory=dict)
    transitions: dict[str, TransitionConfig] = field(default_factory=dict)

    def required_for(self, issue_type: str) -> tuple[str, ...]:
        wanted = issue_type.casefold()
        return next((r for t, r in self.create_required.items() if t.casefold() == wanted), ())


@dataclass(frozen=True)
class ServiceConfig:
    base_url: str
    ca_bundle: Path | None


@dataclass(frozen=True)
class JiraConfig(ServiceConfig):
    max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES
    fields: dict[str, FieldRef] = field(default_factory=dict)
    projects: dict[str, ProjectConfig] = field(default_factory=dict)

    def project(self, key: str) -> ProjectConfig | None:
        return self.projects.get(key)

    def aliases(self, project: str | None) -> dict[str, FieldRef]:
        configured = self.projects.get(project) if project else None
        return {**self.fields, **(configured.fields if configured else {})}


@dataclass(frozen=True)
class ConfluenceConfig(ServiceConfig):
    pass


@dataclass(frozen=True)
class BitbucketConfig(ServiceConfig):
    pass


@dataclass(frozen=True)
class Config:
    path: Path
    version: int
    jira: JiraConfig | None
    confluence: ConfluenceConfig | None
    bitbucket: BitbucketConfig | None = None


# --------------------------------------------------------------------------- YAML loader


class StrictSafeLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys, anchors/aliases, and YAML 1.1 yes/no booleans."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()  # type: ignore[no-untyped-call]
            raise yaml.constructor.ConstructorError(
                None, None, "YAML aliases are not allowed in yojo.yaml", event.start_mark
            )
        event = self.peek_event()  # type: ignore[no-untyped-call]
        if getattr(event, "anchor", None) is not None:
            raise yaml.constructor.ConstructorError(
                None, None, "YAML anchors are not allowed in yojo.yaml", event.start_mark
            )
        return super().compose_node(parent, index)

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        if isinstance(node, yaml.MappingNode):
            seen: set[Any] = set()
            for key_node, _ in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    raise yaml.constructor.ConstructorError(
                        None, None, "YAML merge keys are not allowed", key_node.start_mark
                    )
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, str):
                    raise yaml.constructor.ConstructorError(
                        None, None, "mapping keys must be strings", key_node.start_mark
                    )
                if key in seen:
                    raise yaml.constructor.ConstructorError(
                        None, None, f"duplicate key {key!r}", key_node.start_mark
                    )
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


# Treat only true/false as booleans so option values such as `Yes`/`No`/`On` stay strings.
StrictSafeLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:bool"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
StrictSafeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


# --------------------------------------------------------------------------- discovery


def discover_config(start: Path) -> Path | None:
    """Return the nearest ``yojo.yaml``; a nearer lone ``atl.yaml`` is refused, never migrated."""
    current = start.resolve()
    for directory in (current, *current.parents):
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
        legacy = directory / LEGACY_FILENAME
        if legacy.is_file():
            raise legacy_config(legacy)
    return None


def legacy_config(path: Path) -> YojoError:
    target = path.parent / CONFIG_FILENAME
    return YojoError(
        "config_legacy",
        f"Found {path} from atl, but no {CONFIG_FILENAME}; nothing was read or changed.",
        LEGACY_FIX.format(path=path, target=target),
        {"path": str(path), "expected": str(target)},
    )


def load_config(explicit: str | None, cwd: Path) -> Config | None:
    """Load ``--config PATH`` (which must exist) or the discovered file; None means no config."""
    if explicit is not None:
        path = Path(explicit)
        if not path.is_absolute():
            path = cwd / path
        if not path.is_file():
            raise YojoError(
                "config_not_found",
                f"Config file not found: {path}",
                f"Pass --config with the path to an existing {CONFIG_FILENAME}, or drop --config "
                "and pass --base-url to run without one.",
                {"path": str(path)},
            )
        return parse_config_file(path)
    found = discover_config(cwd)
    return parse_config_file(found) if found is not None else None


def parse_config_file(path: Path) -> Config:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError as exc:
        raise _invalid(path, f"cannot read config: {exc.strerror or type(exc).__name__}") from None
    if len(raw) > MAX_CONFIG_BYTES:
        raise _invalid(path, f"config exceeds {MAX_CONFIG_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid(path, "config is not valid UTF-8") from None
    try:
        document = yaml.load(text, Loader=StrictSafeLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise _invalid(path, _yaml_message(exc)) from None
    return validate_config(document, path)


def _yaml_message(exc: yaml.YAMLError) -> str:
    problem = getattr(exc, "problem", None) or "malformed YAML"
    mark = getattr(exc, "problem_mark", None)
    if mark is not None:
        return f"{problem} (line {mark.line + 1}, column {mark.column + 1})"
    return str(problem)


def _invalid(path: Path, problem: str, where: str | None = None) -> YojoError:
    location = f"{where}: " if where else ""
    return YojoError(
        "config_invalid",
        f"Invalid {path.name}: {location}{problem}",
        f"Edit {path} so that {where or 'the document'} is valid; see docs/configuration.md.",
        {"path": str(path), "where": where, "problem": problem},
    )


# --------------------------------------------------------------------------- URLs


def url_problem(text: str) -> tuple[str | None, str | None]:
    """Return ``(normalized URL, None)`` or ``(None, problem)`` under the strict HTTPS rules."""
    if any(ord(ch) < 32 for ch in text) or len(text) > 2048 or not text.strip():
        return None, "must be a single-line URL of at most 2048 characters"
    parts = urlsplit(text.strip())
    if parts.scheme != "https":
        return None, "must use https://"
    if not parts.hostname:
        return None, "must include a host"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return None, "must not contain credentials (userinfo)"
    if parts.query or text.rstrip().endswith("?"):
        return None, "must not contain a query string"
    if parts.fragment or "#" in text:
        return None, "must not contain a fragment"
    try:
        port = parts.port
    except ValueError:
        return None, "has an invalid port"
    host = parts.hostname.lower()
    netloc = host if port in (None, 443) else f"{host}:{port}"
    if ":" in host:
        netloc = f"[{host}]" if port in (None, 443) else f"[{host}]:{port}"
    return urlunsplit(("https", netloc, parts.path.rstrip("/"), "", "")), None


def normalize_base_url(text: str) -> str:
    """Validate a ``--base-url`` value; it is never a place for credentials."""
    normalized, problem = url_problem(text)
    if normalized is None:
        raise YojoError(
            "usage_error",
            f"--base-url {problem}.",
            "Pass the HTTPS application root including any context path, for example "
            "--base-url https://jira.example.com/jira.",
            {"problem": problem},
        )
    return normalized


# --------------------------------------------------------------------------- validation


class _Validator:
    def __init__(self, path: Path) -> None:
        self.path = path

    def fail(self, where: str, problem: str) -> YojoError:
        return _invalid(self.path, problem, where)

    def mapping(
        self, value: Any, where: str, allowed: set[str], required: set[str]
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise self.fail(where, "must be a mapping")
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise self.fail(where, f"unknown key(s): {', '.join(unknown)}")
        missing = sorted(required - set(value))
        if missing:
            raise self.fail(where, f"missing required key(s): {', '.join(missing)}")
        return value

    def string(self, value: Any, where: str, *, max_len: int = 255) -> str:
        if not isinstance(value, str) or not value.strip():
            raise self.fail(where, "must be a non-empty string")
        if len(value) > max_len:
            raise self.fail(where, f"must be at most {max_len} characters")
        if any(ord(ch) < 32 for ch in value):
            raise self.fail(where, "must not contain control characters")
        return value

    def string_list(self, value: Any, where: str, *, allow_empty: bool = False) -> tuple[str, ...]:
        if not isinstance(value, list) or (not value and not allow_empty):
            raise self.fail(
                where, "must be a non-empty list" if not allow_empty else "must be a list"
            )
        items = tuple(self.string(item, f"{where}[{i}]") for i, item in enumerate(value))
        if len({item.casefold() for item in items}) != len(items):
            raise self.fail(where, "must not contain duplicates")
        return items

    def boolean(self, value: Any, where: str) -> bool:
        if type(value) is not bool:
            raise self.fail(where, "must be true or false")
        return value

    def url(self, value: Any, where: str) -> str:
        normalized, problem = url_problem(self.string(value, where, max_len=2048))
        if normalized is None:
            raise self.fail(where, str(problem))
        return normalized

    def ca_bundle(self, value: Any, where: str) -> Path:
        text = self.string(value, where, max_len=4096)
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = self.path.parent / candidate
        if not candidate.is_file():
            raise self.fail(where, f"CA bundle file does not exist: {candidate}")
        return candidate


def validate_config(document: Any, path: Path) -> Config:
    v = _Validator(path)
    root = v.mapping(document, "<root>", {"version", *SERVICES}, {"version"})
    version = root["version"]
    if type(version) is not int or version not in SUPPORTED_VERSIONS:
        raise v.fail("version", f"unsupported version {version!r}; supported: 1")
    if not any(service in root for service in SERVICES):
        raise v.fail("<root>", "at least one of jira, confluence, or bitbucket must be configured")

    jira = _validate_jira(v, root["jira"]) if "jira" in root else None
    confluence = None
    if "confluence" in root:
        url, ca_bundle = _validate_plain_service(v, root["confluence"], "confluence")
        confluence = ConfluenceConfig(base_url=url, ca_bundle=ca_bundle)
    bitbucket = None
    if "bitbucket" in root:
        url, ca_bundle = _validate_plain_service(v, root["bitbucket"], "bitbucket")
        bitbucket = BitbucketConfig(base_url=url, ca_bundle=ca_bundle)
    return Config(path=path, version=version, jira=jira, confluence=confluence, bitbucket=bitbucket)


def _validate_plain_service(v: _Validator, value: Any, where: str) -> tuple[str, Path | None]:
    section = v.mapping(value, where, {"base_url", "ca_bundle"}, {"base_url"})
    ca_bundle = (
        v.ca_bundle(section["ca_bundle"], f"{where}.ca_bundle") if "ca_bundle" in section else None
    )
    return v.url(section["base_url"], f"{where}.base_url"), ca_bundle


def _validate_jira(v: _Validator, value: Any) -> JiraConfig:
    section = v.mapping(
        value,
        "jira",
        {"base_url", "ca_bundle", "max_attachment_bytes", "fields", "projects"},
        {"base_url"},
    )
    max_bytes = section.get("max_attachment_bytes", DEFAULT_MAX_ATTACHMENT_BYTES)
    if type(max_bytes) is not int or max_bytes <= 0:
        raise v.fail("jira.max_attachment_bytes", "must be a positive integer number of bytes")

    raw_projects = section.get("projects", [])
    if not isinstance(raw_projects, list):
        raise v.fail("jira.projects", "must be a list")
    projects: dict[str, ProjectConfig] = {}
    for index, raw in enumerate(raw_projects):
        project = _validate_project(v, raw, f"jira.projects[{index}]")
        if project.key in projects:
            raise v.fail(f"jira.projects[{index}].key", f"duplicate project key {project.key}")
        projects[project.key] = project

    return JiraConfig(
        base_url=v.url(section["base_url"], "jira.base_url"),
        ca_bundle=v.ca_bundle(section["ca_bundle"], "jira.ca_bundle")
        if "ca_bundle" in section
        else None,
        max_attachment_bytes=max_bytes,
        fields=_validate_fields(v, section.get("fields", {}), "jira.fields"),
        projects=projects,
    )


def _validate_project(v: _Validator, value: Any, where: str) -> ProjectConfig:
    section = v.mapping(value, where, {"key", "fields", "create", "transitions"}, {"key"})
    key = v.string(section["key"], f"{where}.key", max_len=32)
    if not PROJECT_KEY_RE.fullmatch(key):
        raise v.fail(f"{where}.key", "must be an uppercase Jira project key")
    fields = _validate_fields(v, section.get("fields", {}), f"{where}.fields")

    create_required: dict[str, tuple[str, ...]] = {}
    raw_create = section.get("create", {})
    if not isinstance(raw_create, dict):
        raise v.fail(f"{where}.create", "must be a mapping of issue type names to rules")
    for type_name, raw in raw_create.items():
        c_where = f"{where}.create.{type_name}"
        v.string(type_name, c_where)
        if type_name.casefold() in {t.casefold() for t in create_required}:
            raise v.fail(c_where, "duplicates another issue type (names are case-insensitive)")
        entry = v.mapping(raw, c_where, {"required"}, {"required"})
        create_required[type_name] = v.string_list(
            entry["required"], f"{c_where}.required", allow_empty=True
        )

    transitions: dict[str, TransitionConfig] = {}
    raw_transitions = section.get("transitions", {})
    if not isinstance(raw_transitions, dict):
        raise v.fail(f"{where}.transitions", "must be a mapping")
    for name, raw in raw_transitions.items():
        t_where = f"{where}.transitions.{name}"
        if not isinstance(name, str) or not FRIENDLY_NAME_RE.match(name):
            raise v.fail(t_where, "transition names must be lowercase snake_case")
        transitions[name] = _validate_transition(v, name, raw, t_where)
    _reject_duplicate_destinations(v, transitions, f"{where}.transitions")

    return ProjectConfig(
        key=key, fields=fields, create_required=create_required, transitions=transitions
    )


def _validate_fields(v: _Validator, value: Any, where: str) -> dict[str, FieldRef]:
    if not isinstance(value, dict):
        raise v.fail(where, "must be a mapping of friendly names to field references")
    fields: dict[str, FieldRef] = {}
    for friendly, raw in value.items():
        f_where = f"{where}.{friendly}"
        if not isinstance(friendly, str) or not FRIENDLY_NAME_RE.match(friendly):
            raise v.fail(f_where, "friendly names must be lowercase snake_case")
        entry = v.mapping(raw, f_where, {"jira_name", "id"}, set())
        has_name, has_id = "jira_name" in entry, "id" in entry
        if has_name == has_id:
            raise v.fail(f_where, "must set exactly one of jira_name or id")
        field_id = None
        if has_id:
            field_id = v.string(entry["id"], f"{f_where}.id", max_len=64)
            if not FIELD_ID_RE.match(field_id):
                raise v.fail(f"{f_where}.id", "must be a Jira field ID such as customfield_10001")
        fields[friendly] = FieldRef(
            friendly=friendly,
            jira_name=v.string(entry["jira_name"], f"{f_where}.jira_name") if has_name else None,
            id=field_id,
        )
    return fields


def _validate_transition(v: _Validator, name: str, value: Any, where: str) -> TransitionConfig:
    section = v.mapping(
        value, where, {"from", "to", "pre_edit", "auto_set", "require"}, {"from", "to"}
    )
    from_statuses = v.string_list(section["from"], f"{where}.from")
    to = v.string(section["to"], f"{where}.to")
    pre_edit = v.boolean(section.get("pre_edit", False), f"{where}.pre_edit")

    auto_set: dict[str, str | list[str]] = {}
    raw_auto = section.get("auto_set", {})
    if not isinstance(raw_auto, dict):
        raise v.fail(f"{where}.auto_set", "must be a mapping of field names to values")
    for field_name, raw_value in raw_auto.items():
        a_where = f"{where}.auto_set.{field_name}"
        v.string(field_name, a_where)
        if isinstance(raw_value, list):
            auto_set[field_name] = list(v.string_list(raw_value, a_where))
        else:
            auto_set[field_name] = v.string(raw_value, a_where, max_len=32767)

    raw_require = section.get("require", [])
    if not isinstance(raw_require, list):
        raise v.fail(f"{where}.require", "must be a list")
    require: list[Requirement] = []
    for index, raw in enumerate(raw_require):
        r_where = f"{where}.require[{index}]"
        if not isinstance(raw, dict):
            raise v.fail(r_where, "must be a mapping")
        kind = v.string(raw.get("kind"), f"{r_where}.kind")
        if kind not in REQUIREMENT_KINDS:
            raise v.fail(
                f"{r_where}.kind",
                f"unsupported kind {kind!r}; supported: {', '.join(sorted(REQUIREMENT_KINDS))}",
            )
        if kind == "attachment_filename":
            entry = v.mapping(raw, r_where, {"kind", "pattern"}, {"kind", "pattern"})
            pattern = v.string(entry["pattern"], f"{r_where}.pattern", max_len=200)
            _validate_glob(v, pattern, f"{r_where}.pattern")
            require.append(Requirement(kind=kind, pattern=pattern))
        else:
            entry = v.mapping(raw, r_where, {"kind", "field"}, {"kind", "field"})
            require.append(
                Requirement(kind=kind, field=v.string(entry["field"], f"{r_where}.field"))
            )

    return TransitionConfig(
        name=name,
        from_statuses=from_statuses,
        to=to,
        pre_edit=pre_edit,
        auto_set=auto_set,
        require=tuple(require),
    )


def _validate_glob(v: _Validator, pattern: str, where: str) -> None:
    if "/" in pattern or "\\" in pattern:
        raise v.fail(where, "must match a file basename, not a path")
    if not PATTERN_ALLOWED_RE.match(pattern):
        raise v.fail(where, "contains unsupported characters")
    stripped = pattern.replace("{key}", "")
    if "{" in stripped or "}" in stripped:
        raise v.fail(where, "the only supported placeholder is {key}")
    if stripped.count("[") != stripped.count("]"):
        raise v.fail(where, "has unbalanced [ ]")
    try:
        re.compile(fnmatch.translate(stripped))
    except re.error:
        raise v.fail(where, "is not a valid glob") from None


def _reject_duplicate_destinations(
    v: _Validator, transitions: dict[str, TransitionConfig], where: str
) -> None:
    seen: dict[tuple[str, str], str] = {}
    for transition in transitions.values():
        for source in transition.from_statuses:
            pair = (source.casefold(), transition.to.casefold())
            if pair in seen:
                raise v.fail(
                    f"{where}.{transition.name}",
                    f"duplicates transition {seen[pair]!r} from {source!r} to {transition.to!r}",
                )
            seen[pair] = transition.name


# --------------------------------------------------------------------------- service selection


def _service_section(config: Config | None, service: str) -> ServiceConfig | None:
    if config is None:
        return None
    sections: dict[str, ServiceConfig | None] = {
        "jira": config.jira,
        "confluence": config.confluence,
        "bitbucket": config.bitbucket,
    }
    return sections[service]


def select_service(config: Config | None, service: str, base_url: str | None) -> ServiceConfig:
    """Pick the service URL: ``--base-url`` first, then the config; never silently switch.

    The keyring credential is keyed by service and URL, so a conflicting pair is refused rather
    than sending one instance's credential to the other.
    """
    configured = _service_section(config, service)
    if base_url is None:
        if configured is None:
            where = f" in {config.path}" if config is not None else ""
            raise YojoError(
                "service_not_configured",
                f"No {service} URL is set{where}.",
                f"Pass --base-url https://{service}.example.com (your {service} application root "
                f"including any context path), or add {service}.base_url to {CONFIG_FILENAME}.",
                {"service": service},
            )
        return configured
    explicit = normalize_base_url(base_url)
    if configured is None:
        if service == "jira":
            return JiraConfig(base_url=explicit, ca_bundle=None)
        if service == "bitbucket":
            return BitbucketConfig(base_url=explicit, ca_bundle=None)
        return ConfluenceConfig(base_url=explicit, ca_bundle=None)
    if configured.base_url != explicit:
        assert config is not None
        raise YojoError(
            "input_invalid",
            f"--base-url {explicit} differs from {service}.base_url {configured.base_url} in "
            f"{config.path}; nothing was sent.",
            f"Drop --base-url to use {configured.base_url}, or pass --config with a "
            f"{CONFIG_FILENAME} whose {service}.base_url is {explicit}.",
            {"service": service, "base_url": explicit, "configured": configured.base_url},
        )
    return configured
