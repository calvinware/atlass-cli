from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import JIRA, Harness, config_text, jira_path
from yojo.config import (
    DEFAULT_MAX_ATTACHMENT_BYTES,
    BitbucketConfig,
    JiraConfig,
    discover_config,
    load_config,
    parse_config_file,
    select_service,
)
from yojo.contracts import YojoError

ROOT = Path(__file__).resolve().parents[1]


def _write(tmp_path: Path, text: str, name: str = "yojo.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def _error(tmp_path: Path, text: str) -> YojoError:
    with pytest.raises(YojoError) as info:
        parse_config_file(_write(tmp_path, text))
    assert info.value.code == "config_invalid"
    assert info.value.exit_code == 2
    return info.value


def test_example_config_is_valid() -> None:
    config = parse_config_file(ROOT / "yojo.example.yaml")
    assert config.jira is not None and config.confluence is not None
    assert config.jira.base_url == "https://jira.example.test/jira"
    assert config.jira.max_attachment_bytes == 52428800
    assert config.jira.fields["points"].jira_name == "Story Points"
    project = config.jira.projects["EXAMPLE"]
    assert project.required_for("bug") == ("Environment",)
    # YAML 1.1 would turn `Yes` into True; the strict loader keeps option values as strings.
    assert project.transitions["start"].auto_set == {"Review Flag": "Yes"}
    kinds = [r.kind for r in project.transitions["finish"].require]
    assert kinds == ["field_set", "attachment_filename"]
    assert project.transitions["finish"].require[1].expand("EXAMPLE-1") == "EXAMPLE-1_Evidence_*"


def test_no_config_is_allowed(tmp_path: Path) -> None:
    assert load_config(None, tmp_path) is None


def test_lone_legacy_config_is_refused_not_migrated(tmp_path: Path) -> None:
    legacy = _write(tmp_path, "version: 1\njira: {}\n", "atl.yaml")
    before = legacy.read_bytes()
    nested = tmp_path / "sub"
    nested.mkdir()
    with pytest.raises(YojoError) as info:
        load_config(None, nested)
    assert info.value.code == "config_legacy"
    assert info.value.exit_code == 2
    assert "Rename" in info.value.fix and "yojo.yaml" in info.value.fix
    assert "never migrates" in info.value.fix
    assert legacy.read_bytes() == before
    assert not (tmp_path / "yojo.yaml").exists()


def test_yojo_config_wins_over_legacy_in_the_same_directory(tmp_path: Path) -> None:
    _write(tmp_path, "garbage: [", "atl.yaml")
    _write(tmp_path, config_text())
    config = load_config(None, tmp_path)
    assert config is not None and config.path.name == "yojo.yaml"


def test_legacy_config_through_cli(harness: Harness) -> None:
    (harness.tmp_path / "atl.yaml").write_text("version: 1\n")
    result = harness.run("jira", "search", "--jql", "project = EXAMPLE")
    assert result.code == 2
    assert result.error["code"] == "config_legacy"
    assert harness.server.requests == []


def test_offline_commands_ignore_a_legacy_config(harness: Harness) -> None:
    (harness.tmp_path / "atl.yaml").write_text("version: 1\n")
    assert harness.run("commands").code == 0
    assert harness.run("schema", "jira issue create").code == 0


def test_base_url_without_config_selects_the_matching_credential(harness: Harness) -> None:
    harness.store()
    harness.server.add("GET", jira_path("/rest/api/2/search"), {"issues": [], "total": 0})
    result = harness.run("jira", "search", "--jql", "x", "--base-url", JIRA + "/")
    assert result.code == 0
    assert harness.server.requests[0].url.host == "jira.example.test"


def test_base_url_is_validated_before_anything_is_sent(harness: Harness) -> None:
    harness.store()
    for bad in ("http://jira.example.test", "https://u:p@jira.example.test", "https://x/?q=1"):
        result = harness.run("jira", "search", "--jql", "x", "--base-url", bad)
        assert result.error["code"] == "usage_error"
        assert "u:p" not in result.stdout
    assert harness.server.requests == []


def test_base_url_conflicting_with_config_is_refused(harness: Harness) -> None:
    harness.write_config()
    harness.store()
    result = harness.run(
        "jira", "search", "--jql", "x", "--base-url", "https://other.example.test/jira"
    )
    assert result.code == 2
    assert result.error["code"] == "input_invalid"
    assert harness.server.requests == []


def test_base_url_matching_config_keeps_config_rules(tmp_path: Path) -> None:
    config = parse_config_file(_write(tmp_path, config_text()))
    selected = select_service(config, "jira", JIRA + "/")
    assert isinstance(selected, JiraConfig) and "EXAMPLE" in selected.projects


def test_no_url_anywhere_says_how_to_fix(harness: Harness) -> None:
    result = harness.run("jira", "issue", "get", "EXAMPLE-1")
    assert result.code == 2
    assert result.error["code"] == "service_not_configured"
    assert "--base-url" in result.error["fix"]


def test_config_with_only_base_url_is_valid(tmp_path: Path) -> None:
    config = parse_config_file(
        _write(tmp_path, "version: 1\njira:\n  base_url: https://j.example.test\n")
    )
    assert config.jira is not None and config.jira.projects == {} and config.jira.fields == {}


def test_url_normalization_preserves_context_path(tmp_path: Path) -> None:
    config = parse_config_file(_write(tmp_path, config_text()))
    assert config.jira is not None
    assert config.jira.base_url == "https://jira.example.test/jira"


def test_discovery_walks_up(tmp_path: Path) -> None:
    _write(tmp_path, config_text())
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert discover_config(nested) == (tmp_path / "yojo.yaml").resolve()
    loaded = load_config(None, nested)
    assert loaded is not None and loaded.path == (tmp_path / "yojo.yaml").resolve()


def test_explicit_config_overrides_discovery(tmp_path: Path) -> None:
    _write(tmp_path, "not: [valid")
    other = tmp_path / "other.yaml"
    other.write_text(config_text())
    loaded = load_config("other.yaml", tmp_path)
    assert loaded is not None and loaded.path == other


def test_missing_explicit_config(tmp_path: Path) -> None:
    with pytest.raises(YojoError) as info:
        load_config("missing.yaml", tmp_path)
    assert info.value.code == "config_not_found"


def test_default_max_attachment_bytes(tmp_path: Path) -> None:
    text = config_text().replace("  max_attachment_bytes: 52428800\n", "")
    config = parse_config_file(_write(tmp_path, text))
    assert config.jira is not None
    assert config.jira.max_attachment_bytes == DEFAULT_MAX_ATTACHMENT_BYTES == 52428800


@pytest.mark.parametrize("value", ["true", "1.5", "'100'", "0", "-1", "5.0e7", "null"])
def test_max_attachment_bytes_rejects(tmp_path: Path, value: str) -> None:
    text = config_text().replace("max_attachment_bytes: 52428800", f"max_attachment_bytes: {value}")
    error = _error(tmp_path, text)
    assert error.details["where"] == "jira.max_attachment_bytes"


def test_max_attachment_bytes_accepts_positive_int(tmp_path: Path) -> None:
    config = parse_config_file(_write(tmp_path, config_text(max_bytes=1)))
    assert config.jira is not None and config.jira.max_attachment_bytes == 1


@pytest.mark.parametrize("value", ["yes", "'false'", "1", "null"])
def test_pre_edit_must_be_strict_bool(tmp_path: Path, value: str) -> None:
    error = _error(tmp_path, config_text(pre_edit=value))
    assert error.details["where"].endswith("in_progress.pre_edit")


def test_pre_edit_defaults_false(tmp_path: Path) -> None:
    config = parse_config_file(_write(tmp_path, config_text()))
    assert config.jira is not None
    assert config.jira.projects["EXAMPLE"].transitions["done"].pre_edit is False


def test_pre_edit_true(tmp_path: Path) -> None:
    config = parse_config_file(_write(tmp_path, config_text(pre_edit="true")))
    assert config.jira is not None
    assert config.jira.projects["EXAMPLE"].transitions["in_progress"].pre_edit is True


@pytest.mark.parametrize(
    ("old", "new", "where"),
    [
        ("version: 1", "version: 2", "version"),
        ("version: 1", "version: true", "version"),
        ("version: 1", "version: 1\nextra: 1", "<root>"),
        ("    - key: EXAMPLE", "    - key: EXAMPLE\n      colour: red", "jira.projects[0]"),
        (
            "    - key: EXAMPLE",
            "    - key: EXAMPLE\n      story_type: Story",
            "jira.projects[0]",
        ),
        (
            "base_url: https://jira.example.test/jira/",
            "base_url: http://jira.example.test/jira",
            "jira.base_url",
        ),
        (
            "base_url: https://jira.example.test/jira/",
            "base_url: https://u:p@jira.example.test",
            "jira.base_url",
        ),
        (
            "base_url: https://jira.example.test/jira/",
            "base_url: https://jira.example.test/?a=1",
            "jira.base_url",
        ),
        (
            "base_url: https://jira.example.test/jira/",
            "base_url: 'https://jira.example.test/#x'",
            "jira.base_url",
        ),
        (
            "{jira_name: Review Flag}",
            "{jira_name: Review Flag, encoding: single_option}",
            "review_flag",
        ),
        ("{jira_name: Review Flag}", "{jira_name: WS, id: customfield_1}", "review_flag"),
        ("{jira_name: Review Flag}", "{id: 'bad id'}", ".id"),
        ("    review_flag:", "    Review_Flag:", "jira.fields.Review_Flag"),
        ('pattern: "{key}_Evidence_*"', 'pattern: "results/{key}_*"', "pattern"),
        ('pattern: "{key}_Evidence_*"', 'pattern: "{issue}_*"', "pattern"),
        ('pattern: "{key}_Evidence_*"', 'pattern: "{key}_[abc"', "pattern"),
        ("kind: attachment_filename", "kind: field_present", "kind"),
        (
            '- kind: attachment_filename\n              pattern: "{key}_Evidence_*"',
            "- kind: field_set",
            "require[0]",
        ),
        (
            '- kind: attachment_filename\n              pattern: "{key}_Evidence_*"',
            "- kind: field_set\n              pattern: x",
            "require[0]",
        ),
    ],
)
def test_rejects_invalid_config(tmp_path: Path, old: str, new: str, where: str) -> None:
    text = config_text()
    assert old in text
    error = _error(tmp_path, text.replace(old, new, 1))
    assert where in (error.details["where"] or "") + error.details["problem"]
    assert error.fix


@pytest.mark.parametrize("value", ["[attachment_filename]", "{a: b}", "1", "null"])
def test_rejects_non_string_requirement_kind(tmp_path: Path, value: str) -> None:
    text = config_text().replace("kind: attachment_filename", f"kind: {value}")
    error = _error(tmp_path, text)
    assert error.details["where"] == "jira.projects[0].transitions.done.require[0].kind"


def test_field_set_rule(tmp_path: Path) -> None:
    text = config_text().replace(
        '- kind: attachment_filename\n              pattern: "{key}_Evidence_*"',
        "- kind: field_set\n              field: Resolution",
    )
    config = parse_config_file(_write(tmp_path, text))
    assert config.jira is not None
    rule = config.jira.projects["EXAMPLE"].transitions["done"].require[0]
    assert (rule.kind, rule.field) == ("field_set", "Resolution")


def test_non_string_enum_is_config_invalid_through_cli(harness: Harness) -> None:
    harness.write_config(config_text().replace("kind: attachment_filename", "kind: [x]"))
    result = harness.run("jira", "search", "--jql", "project = EXAMPLE")
    assert result.code == 2
    assert result.error["code"] == "config_invalid"
    assert result.error["details"]["where"].endswith("require[0].kind")


CREATE_ANCHOR = "      transitions:\n"


def _with_create(block: str) -> str:
    text = config_text()
    assert CREATE_ANCHOR in text
    return text.replace(CREATE_ANCHOR, block + CREATE_ANCHOR, 1)


@pytest.mark.parametrize(
    ("block", "issue_type", "required"),
    [
        ("      create:\n        Bug:\n          required: [Severity]\n", "bug", ("Severity",)),
        ("      create:\n        Bug:\n          required: []\n", "Bug", ()),
        ("      create: {}\n", "Bug", ()),
        ("", "Bug", ()),
    ],
)
def test_create_requirements_per_type(
    tmp_path: Path, block: str, issue_type: str, required: tuple[str, ...]
) -> None:
    config = parse_config_file(_write(tmp_path, _with_create(block)))
    assert config.jira is not None
    assert config.jira.projects["EXAMPLE"].required_for(issue_type) == required


@pytest.mark.parametrize(
    ("block", "where", "problem"),
    [
        (
            "      create: [Bug]\n",
            "jira.projects[0].create",
            "must be a mapping of issue type names to rules",
        ),
        (
            "      create:\n        Bug: [Severity]\n",
            "jira.projects[0].create.Bug",
            "must be a mapping",
        ),
        (
            "      create:\n        Bug:\n          required: Severity\n",
            "jira.projects[0].create.Bug.required",
            "must be a list",
        ),
        (
            "      create:\n        Bug:\n          required: [A, a]\n",
            "jira.projects[0].create.Bug.required",
            "must not contain duplicates",
        ),
        (
            "      create:\n        Bug:\n          required: [7]\n",
            "jira.projects[0].create.Bug.required[0]",
            "must be a non-empty string",
        ),
        (
            "      create:\n        Bug:\n          optional: []\n",
            "jira.projects[0].create.Bug",
            "unknown key(s): optional",
        ),
        (
            "      create:\n        Bug:\n          required: []\n"
            "        bug:\n          required: []\n",
            "jira.projects[0].create.bug",
            "duplicates another issue type (names are case-insensitive)",
        ),
    ],
)
def test_rejects_invalid_create_requirements(
    tmp_path: Path, block: str, where: str, problem: str
) -> None:
    error = _error(tmp_path, _with_create(block))
    assert error.details["where"] == where
    assert error.details["problem"] == problem
    assert where in error.fix


def test_rejects_duplicate_project_keys(tmp_path: Path) -> None:
    text = config_text()
    start = text.index("    - key: EXAMPLE")
    end = text.index("confluence:")
    project = text[start:end]
    error = _error(tmp_path, text[:end] + project + text[end:])
    assert "duplicate project key" in error.details["problem"]


def test_rejects_duplicate_yaml_keys(tmp_path: Path) -> None:
    error = _error(tmp_path, config_text().replace("version: 1", "version: 1\nversion: 1"))
    assert "duplicate key" in error.details["problem"]


def test_rejects_anchors_and_aliases(tmp_path: Path) -> None:
    text = config_text().replace("from: [To Do]", "from: &t [To Do]")
    assert "anchor" in _error(tmp_path, text).details["problem"]


def test_rejects_duplicate_destination_from_same_source(tmp_path: Path) -> None:
    text = config_text().replace(
        "        done:\n",
        "        start_again:\n          from: [To Do]\n          to: In Progress\n        done:\n",
    )
    assert "duplicates transition" in _error(tmp_path, text).details["problem"]


def test_rejects_malformed_yaml(tmp_path: Path) -> None:
    assert "line" in _error(tmp_path, "version: [1\n").details["problem"]


def test_rejects_python_tags(tmp_path: Path) -> None:
    _error(tmp_path, "version: !!python/object/apply:os.system ['echo hi']\n")


def test_rejects_oversized_config(tmp_path: Path) -> None:
    assert "exceeds" in _error(tmp_path, config_text() + "#" * 300_000).details["problem"]


def test_missing_ca_bundle(tmp_path: Path) -> None:
    text = config_text().replace(
        "  max_attachment_bytes", "  ca_bundle: missing.pem\n  max_attachment_bytes"
    )
    assert "CA bundle" in _error(tmp_path, text).details["problem"]


def test_existing_ca_bundle_resolved_relative_to_config(tmp_path: Path) -> None:
    (tmp_path / "ca.pem").write_text("placeholder")
    text = config_text().replace(
        "  max_attachment_bytes", "  ca_bundle: ca.pem\n  max_attachment_bytes"
    )
    config = parse_config_file(_write(tmp_path, text))
    assert config.jira is not None and config.jira.ca_bundle == tmp_path / "ca.pem"


def test_service_sections_are_optional(tmp_path: Path) -> None:
    config = parse_config_file(
        _write(tmp_path, "version: 1\nconfluence:\n  base_url: https://wiki.example.test\n")
    )
    assert config.jira is None and config.confluence is not None
    _error(tmp_path, "version: 1\n")


def test_explicit_field_id(tmp_path: Path) -> None:
    text = config_text().replace("{jira_name: Review Flag}", "{id: customfield_10100}")
    config = parse_config_file(_write(tmp_path, text))
    assert config.jira is not None
    assert config.jira.fields["review_flag"].id == "customfield_10100"


def test_project_aliases_override_global_aliases(tmp_path: Path) -> None:
    text = config_text().replace(
        "    - key: EXAMPLE\n",
        "    - key: EXAMPLE\n      fields:\n        review_flag: {id: customfield_9}\n",
    )
    config = parse_config_file(_write(tmp_path, text))
    assert config.jira is not None
    assert config.jira.aliases("EXAMPLE")["review_flag"].id == "customfield_9"
    assert config.jira.aliases("OTHER")["review_flag"].jira_name == "Review Flag"


def test_project_key_rejects_trailing_newline(tmp_path: Path) -> None:
    text = config_text().replace("- key: EXAMPLE", '- key: "EXAMPLE\\n"')
    assert '"EXAMPLE\\n"' in text
    _error(tmp_path, text)


def test_bitbucket_only_config(tmp_path: Path) -> None:
    (tmp_path / "ca.pem").write_text("x")
    text = (
        "version: 1\nbitbucket:\n  base_url: https://BITBUCKET.example.test/bitbucket/\n"
        "  ca_bundle: ca.pem\n"
    )
    config = parse_config_file(_write(tmp_path, text))
    assert config.jira is None and config.confluence is None
    assert config.bitbucket == BitbucketConfig(
        base_url="https://bitbucket.example.test/bitbucket", ca_bundle=tmp_path / "ca.pem"
    )
    assert select_service(config, "bitbucket", None) is config.bitbucket
    with pytest.raises(YojoError) as info:
        select_service(config, "jira", None)
    assert info.value.code == "service_not_configured"


def test_mixed_config_keeps_each_service(tmp_path: Path) -> None:
    text = config_text() + "bitbucket:\n  base_url: https://bitbucket.example.test/bitbucket\n"
    config = parse_config_file(_write(tmp_path, text))
    assert config.jira is not None and config.bitbucket is not None
    assert select_service(config, "bitbucket", "https://bitbucket.example.test/bitbucket/")
    with pytest.raises(YojoError) as info:
        select_service(config, "bitbucket", "https://other.example.test/bitbucket")
    assert info.value.code == "input_invalid"


def test_bitbucket_without_config_uses_base_url(tmp_path: Path) -> None:
    selected = select_service(None, "bitbucket", "https://bitbucket.example.test/bb/")
    assert isinstance(selected, BitbucketConfig)
    assert selected.base_url == "https://bitbucket.example.test/bb"


@pytest.mark.parametrize(
    ("section", "where"),
    [
        ("bitbucket:\n  base_url: http://bitbucket.example.test\n", "bitbucket.base_url"),
        ("bitbucket:\n  base_url: https://b.example.test\n  projects: []\n", "bitbucket"),
        ("bitbucket: {}\n", "bitbucket"),
    ],
)
def test_rejects_invalid_bitbucket_section(tmp_path: Path, section: str, where: str) -> None:
    error = _error(tmp_path, "version: 1\n" + section)
    assert error.details["where"] == where


def test_empty_config_names_every_service(tmp_path: Path) -> None:
    error = _error(tmp_path, "version: 1\n")
    assert "jira, confluence, or bitbucket" in error.details["problem"]
