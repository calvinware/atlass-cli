"""Strict, versioned ``atl.yaml`` discovery, parsing, and validation."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

from atlass_cli.contracts import PROJECT_KEY_PATTERN, AtlError

CONFIG_FILENAME = "atl.yaml"
MAX_CONFIG_BYTES = 256 * 1024
DEFAULT_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
SUPPORTED_VERSIONS = {1}

ENCODINGS = {
    "text",
    "issue_key_string",
    "issue_key_object",
    "single_option",
    "multi_option",
    "resolution_option",
}
FEATURE_POLICIES = {"automatic", "human_confirmed"}
REQUIREMENT_KINDS = {"attachment_filename"}
RESERVED_FIELDS = ("acceptance_criteria", "feature_link")
CREATE_TYPES = ("story", "feature")

FRIENDLY_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
FIELD_ID_RE = re.compile(r"^(customfield_[0-9]{1,10}|[a-z][A-Za-z]{0,63})$")
PROJECT_KEY_RE = re.compile(PROJECT_KEY_PATTERN)
PATTERN_ALLOWED_RE = re.compile(r"^[A-Za-z0-9 ._\-*?\[\]!{}()+=,@#~]+$")


@dataclass(frozen=True)
class FieldRef:
    friendly: str
    encoding: str
    jira_name: str | None = None
    id: str | None = None

    @property
    def label(self) -> str:
        return self.jira_name or self.id or self.friendly


@dataclass(frozen=True)
class Requirement:
    kind: str
    pattern: str

    def expand(self, issue_key: str) -> str:
        return self.pattern.replace("{key}", issue_key)


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
    allowed_issue_types: tuple[str, ...]
    story_type: str
    feature_type: str
    feature_create_policy: str
    fields: dict[str, FieldRef]
    story_required: tuple[str, ...]
    feature_required: tuple[str, ...]
    transitions: dict[str, TransitionConfig]


@dataclass(frozen=True)
class ServiceConfig:
    base_url: str
    ca_bundle: Path | None


@dataclass(frozen=True)
class JiraConfig(ServiceConfig):
    max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES
    projects: dict[str, ProjectConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class ConfluenceConfig(ServiceConfig):
    pass


@dataclass(frozen=True)
class Config:
    path: Path
    version: int
    jira: JiraConfig | None
    confluence: ConfluenceConfig | None


# --------------------------------------------------------------------------- YAML loader


class StrictSafeLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys, anchors/aliases, and YAML 1.1 yes/no booleans."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()  # type: ignore[no-untyped-call]
            raise yaml.constructor.ConstructorError(
                None, None, "YAML aliases are not allowed in atl.yaml", event.start_mark
            )
        event = self.peek_event()  # type: ignore[no-untyped-call]
        if getattr(event, "anchor", None) is not None:
            raise yaml.constructor.ConstructorError(
                None, None, "YAML anchors are not allowed in atl.yaml", event.start_mark
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
    current = start.resolve()
    for directory in (current, *current.parents):
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def load_config(explicit: str | None, cwd: Path) -> Config:
    if explicit is not None:
        path = Path(explicit)
        if not path.is_absolute():
            path = cwd / path
        if not path.is_file():
            raise AtlError(
                "config_not_found",
                f"Config file not found: {path}",
                "Pass --config with the path to an existing atl.yaml.",
                {"path": str(path)},
            )
    else:
        found = discover_config(cwd)
        if found is None:
            raise AtlError(
                "config_not_found",
                f"No {CONFIG_FILENAME} found in {cwd} or any parent directory.",
                f"Copy atl.example.yaml to {CONFIG_FILENAME} in the repository root and fill in "
                "team values, or pass --config PATH.",
                {"searched_from": str(cwd)},
            )
        path = found
    return parse_config_file(path)


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


def _invalid(path: Path, problem: str, where: str | None = None) -> AtlError:
    location = f"{where}: " if where else ""
    return AtlError(
        "config_invalid",
        f"Invalid {path.name}: {location}{problem}",
        f"Edit {path} so that {where or 'the document'} is valid; see docs/configuration.md.",
        {"path": str(path), "where": where, "problem": problem},
    )


# --------------------------------------------------------------------------- validation


class _Validator:
    def __init__(self, path: Path) -> None:
        self.path = path

    def fail(self, where: str, problem: str) -> AtlError:
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
        text = self.string(value, where, max_len=2048)
        parts = urlsplit(text)
        if parts.scheme != "https":
            raise self.fail(where, "must use https://")
        if not parts.hostname:
            raise self.fail(where, "must include a host")
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise self.fail(where, "must not contain credentials (userinfo)")
        if parts.query or text.rstrip().endswith("?"):
            raise self.fail(where, "must not contain a query string")
        if parts.fragment or "#" in text:
            raise self.fail(where, "must not contain a fragment")
        try:
            port = parts.port
        except ValueError:
            raise self.fail(where, "has an invalid port") from None
        host = parts.hostname.lower()
        netloc = host if port in (None, 443) else f"{host}:{port}"
        if ":" in host:
            netloc = f"[{host}]" if port in (None, 443) else f"[{host}]:{port}"
        path = parts.path.rstrip("/")
        return urlunsplit(("https", netloc, path, "", ""))

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
    root = v.mapping(document, "<root>", {"version", "jira", "confluence"}, {"version"})
    version = root["version"]
    if type(version) is not int or version not in SUPPORTED_VERSIONS:
        raise v.fail("version", f"unsupported version {version!r}; supported: 1")
    if "jira" not in root and "confluence" not in root:
        raise v.fail("<root>", "at least one of jira or confluence must be configured")

    jira = _validate_jira(v, root["jira"]) if "jira" in root else None
    confluence = None
    if "confluence" in root:
        section = v.mapping(
            root["confluence"], "confluence", {"base_url", "ca_bundle"}, {"base_url"}
        )
        confluence = ConfluenceConfig(
            base_url=v.url(section["base_url"], "confluence.base_url"),
            ca_bundle=v.ca_bundle(section["ca_bundle"], "confluence.ca_bundle")
            if "ca_bundle" in section
            else None,
        )
    return Config(path=path, version=version, jira=jira, confluence=confluence)


def _validate_jira(v: _Validator, value: Any) -> JiraConfig:
    section = v.mapping(
        value,
        "jira",
        {"base_url", "ca_bundle", "max_attachment_bytes", "projects"},
        {"base_url", "projects"},
    )
    max_bytes = section.get("max_attachment_bytes", DEFAULT_MAX_ATTACHMENT_BYTES)
    if type(max_bytes) is not int or max_bytes <= 0:
        raise v.fail("jira.max_attachment_bytes", "must be a positive integer number of bytes")

    raw_projects = section["projects"]
    if not isinstance(raw_projects, list) or not raw_projects:
        raise v.fail("jira.projects", "must be a non-empty list")
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
        projects=projects,
    )


def _validate_project(v: _Validator, value: Any, where: str) -> ProjectConfig:
    section = v.mapping(
        value,
        where,
        {
            "key",
            "allowed_issue_types",
            "story_type",
            "feature_type",
            "feature_create_policy",
            "fields",
            "create",
            "transitions",
        },
        {"key", "allowed_issue_types", "story_type", "feature_type", "fields"},
    )
    key = v.string(section["key"], f"{where}.key", max_len=32)
    if not PROJECT_KEY_RE.fullmatch(key):
        raise v.fail(f"{where}.key", "must be an uppercase Jira project key")
    allowed = v.string_list(section["allowed_issue_types"], f"{where}.allowed_issue_types")
    story_type = v.string(section["story_type"], f"{where}.story_type")
    feature_type = v.string(section["feature_type"], f"{where}.feature_type")
    for name, value_ in (("story_type", story_type), ("feature_type", feature_type)):
        if value_ not in allowed:
            raise v.fail(f"{where}.{name}", f"{value_!r} is not in allowed_issue_types")
    if story_type == feature_type:
        raise v.fail(f"{where}.feature_type", "must differ from story_type")
    policy = v.string(
        section.get("feature_create_policy", "human_confirmed"), f"{where}.feature_create_policy"
    )
    if policy not in FEATURE_POLICIES:
        raise v.fail(f"{where}.feature_create_policy", "must be 'automatic' or 'human_confirmed'")

    fields = _validate_fields(v, section["fields"], f"{where}.fields")

    story_required: tuple[str, ...] = ()
    feature_required: tuple[str, ...] = ()
    if "create" in section:
        create = v.mapping(section["create"], f"{where}.create", set(CREATE_TYPES), set())
        story_required = _validate_create_type(v, create, "story", f"{where}.create", fields)
        feature_required = _validate_create_type(v, create, "feature", f"{where}.create", fields)

    transitions: dict[str, TransitionConfig] = {}
    raw_transitions = section.get("transitions", {})
    if not isinstance(raw_transitions, dict):
        raise v.fail(f"{where}.transitions", "must be a mapping")
    for name, raw in raw_transitions.items():
        t_where = f"{where}.transitions.{name}"
        if not FRIENDLY_NAME_RE.match(name):
            raise v.fail(t_where, "transition names must be lowercase snake_case")
        transitions[name] = _validate_transition(v, name, raw, t_where, fields)
    _reject_duplicate_destinations(v, transitions, f"{where}.transitions")

    return ProjectConfig(
        key=key,
        allowed_issue_types=allowed,
        story_type=story_type,
        feature_type=feature_type,
        feature_create_policy=policy,
        fields=fields,
        story_required=story_required,
        feature_required=feature_required,
        transitions=transitions,
    )


def _validate_create_type(
    v: _Validator, create: dict[str, Any], type_key: str, where: str, fields: dict[str, FieldRef]
) -> tuple[str, ...]:
    if type_key not in create:
        return ()
    t_where = f"{where}.{type_key}"
    entry = v.mapping(create[type_key], t_where, {"required"}, set())
    if "required" not in entry:
        return ()
    required = v.string_list(entry["required"], f"{t_where}.required", allow_empty=True)
    for index, name in enumerate(required):
        if name not in fields:
            raise v.fail(f"{t_where}.required[{index}]", f"unknown field {name!r}")
    return required


def _validate_fields(v: _Validator, value: Any, where: str) -> dict[str, FieldRef]:
    if not isinstance(value, dict):
        raise v.fail(where, "must be a mapping of friendly names to field references")
    fields: dict[str, FieldRef] = {}
    for friendly, raw in value.items():
        f_where = f"{where}.{friendly}"
        if not FRIENDLY_NAME_RE.match(friendly):
            raise v.fail(f_where, "friendly names must be lowercase snake_case")
        entry = v.mapping(raw, f_where, {"jira_name", "id", "encoding"}, {"encoding"})
        has_name, has_id = "jira_name" in entry, "id" in entry
        if has_name == has_id:
            raise v.fail(f_where, "must set exactly one of jira_name or id")
        encoding = v.string(entry["encoding"], f"{f_where}.encoding")
        if encoding not in ENCODINGS:
            raise v.fail(
                f"{f_where}.encoding",
                f"unsupported encoding {encoding!r}; supported: {', '.join(sorted(ENCODINGS))}",
            )
        field_id = None
        if has_id:
            field_id = v.string(entry["id"], f"{f_where}.id", max_len=64)
            if not FIELD_ID_RE.match(field_id):
                raise v.fail(f"{f_where}.id", "must be a Jira field ID such as customfield_10001")
        fields[friendly] = FieldRef(
            friendly=friendly,
            encoding=encoding,
            jira_name=v.string(entry["jira_name"], f"{f_where}.jira_name") if has_name else None,
            id=field_id,
        )
    for reserved in RESERVED_FIELDS:
        if reserved not in fields:
            raise v.fail(where, f"must define the {reserved!r} field used by story create")
    return fields


def _validate_transition(
    v: _Validator, name: str, value: Any, where: str, fields: dict[str, FieldRef]
) -> TransitionConfig:
    section = v.mapping(
        value, where, {"from", "to", "pre_edit", "auto_set", "require"}, {"from", "to"}
    )
    from_statuses = v.string_list(section["from"], f"{where}.from")
    to = v.string(section["to"], f"{where}.to")
    pre_edit = v.boolean(section.get("pre_edit", False), f"{where}.pre_edit")

    auto_set: dict[str, str | list[str]] = {}
    raw_auto = section.get("auto_set", {})
    if not isinstance(raw_auto, dict):
        raise v.fail(f"{where}.auto_set", "must be a mapping of friendly field names to values")
    for friendly, raw_value in raw_auto.items():
        a_where = f"{where}.auto_set.{friendly}"
        if friendly not in fields:
            raise v.fail(a_where, f"unknown field {friendly!r}; define it under fields")
        encoding = fields[friendly].encoding
        if encoding == "multi_option":
            auto_set[friendly] = list(v.string_list(raw_value, a_where))
        else:
            auto_set[friendly] = v.string(raw_value, a_where, max_len=32767)

    raw_require = section.get("require", [])
    if not isinstance(raw_require, list):
        raise v.fail(f"{where}.require", "must be a list")
    require: list[Requirement] = []
    for index, raw in enumerate(raw_require):
        r_where = f"{where}.require[{index}]"
        entry = v.mapping(raw, r_where, {"kind", "pattern"}, {"kind", "pattern"})
        kind = v.string(entry["kind"], f"{r_where}.kind")
        if kind not in REQUIREMENT_KINDS:
            raise v.fail(f"{r_where}.kind", f"unsupported kind {kind!r}")
        pattern = v.string(entry["pattern"], f"{r_where}.pattern", max_len=200)
        _validate_glob(v, pattern, f"{r_where}.pattern")
        require.append(Requirement(kind=kind, pattern=pattern))

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


def require_project(config: Config, key: str) -> ProjectConfig:
    jira = require_jira(config)
    project = jira.projects.get(key)
    if project is None:
        raise AtlError(
            "project_not_configured",
            f"Project {key} is not configured in {config.path.name}.",
            f"Add a jira.projects entry with key {key} to {config.path}, or use one of: "
            f"{', '.join(sorted(jira.projects))}.",
            {"project": key, "configured": sorted(jira.projects)},
        )
    return project


def require_jira(config: Config) -> JiraConfig:
    if config.jira is None:
        raise AtlError(
            "service_not_configured",
            "Jira is not configured.",
            f"Add a jira section with base_url and projects to {config.path}.",
            {"service": "jira"},
        )
    return config.jira


def require_confluence(config: Config) -> ConfluenceConfig:
    if config.confluence is None:
        raise AtlError(
            "service_not_configured",
            "Confluence is not configured.",
            f"Add a confluence section with base_url to {config.path}.",
            {"service": "confluence"},
        )
    return config.confluence
