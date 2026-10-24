from __future__ import annotations

from pathlib import Path

import pytest

from atlass_cli.config import (
    DEFAULT_MAX_ATTACHMENT_BYTES,
    discover_config,
    load_config,
    parse_config_file,
)
from atlass_cli.contracts import AtlError
from tests.conftest import config_text

ROOT = Path(__file__).resolve().parents[1]


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "atl.yaml"
    path.write_text(text)
    return path


def _error(tmp_path: Path, text: str) -> AtlError:
    with pytest.raises(AtlError) as info:
        parse_config_file(_write(tmp_path, text))
    assert info.value.code == "config_invalid"
    assert info.value.exit_code == 2
    return info.value


def test_example_config_is_valid() -> None:
    config = parse_config_file(ROOT / "atl.example.yaml")
    assert config.jira is not None and config.confluence is not None
    assert config.jira.base_url == "https://jira.example.test/jira"
    assert config.jira.max_attachment_bytes == 52428800
    project = config.jira.projects["EXAMPLE"]
    assert project.feature_create_policy == "human_confirmed"
    assert project.transitions["in_progress"].pre_edit is False
    # YAML 1.1 would turn `Yes` into True; the strict loader keeps option values as strings.
    assert project.transitions["in_progress"].auto_set == {"work_started": "Yes"}
    assert project.transitions["done"].require[0].expand("EXAMPLE-1") == "EXAMPLE-1_TestResults_*"
    assert project.story_required == ("acceptance_criteria", "feature_link")
    assert project.feature_required == ()


def test_url_normalization_preserves_context_path(tmp_path: Path) -> None:
    config = parse_config_file(_write(tmp_path, config_text()))
    assert config.jira is not None
    assert config.jira.base_url == "https://jira.example.test/jira"


def test_discovery_walks_up(tmp_path: Path) -> None:
    _write(tmp_path, config_text())
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert discover_config(nested) == (tmp_path / "atl.yaml").resolve()
    assert load_config(None, nested).path == (tmp_path / "atl.yaml").resolve()


def test_explicit_config_overrides_discovery(tmp_path: Path) -> None:
    _write(tmp_path, "not: [valid")
    other = tmp_path / "other.yaml"
    other.write_text(config_text())
    assert load_config("other.yaml", tmp_path).path == other


def test_missing_explicit_config(tmp_path: Path) -> None:
    with pytest.raises(AtlError) as info:
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
        (
            "      story_type: Story",
            "      story_type: Story\n      colour: red",
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
        ("encoding: text}", "encoding: rich_text}", "encoding"),
        (
            "{jira_name: Acceptance Criteria, encoding: text}",
            "{jira_name: AC, id: customfield_1, encoding: text}",
            "acceptance_criteria",
        ),
        (
            "{jira_name: Acceptance Criteria, encoding: text}",
            "{id: 'bad id', encoding: text}",
            ".id",
        ),
        ("auto_set: {work_started: Yes}", "auto_set: {missing: Yes}", "auto_set.missing"),
        ("story_type: Story", "story_type: Epic", "story_type"),
        (
            "        feature_link: {jira_name: Parent Link, encoding: issue_key_string}\n",
            "",
            "fields",
        ),
        ('pattern: "{key}_TestResults_*"', 'pattern: "results/{key}_*"', "pattern"),
        ('pattern: "{key}_TestResults_*"', 'pattern: "{issue}_*"', "pattern"),
        ('pattern: "{key}_TestResults_*"', 'pattern: "{key}_[abc"', "pattern"),
        ("kind: attachment_filename", "kind: field_present", "kind"),
        (
            "feature_create_policy: human_confirmed",
            "feature_create_policy: sometimes",
            "feature_create_policy",
        ),
    ],
)
def test_rejects_invalid_config(tmp_path: Path, old: str, new: str, where: str) -> None:
    text = config_text()
    assert old in text
    error = _error(tmp_path, text.replace(old, new, 1))
    assert where in (error.details["where"] or "") + error.details["problem"]
    assert error.fix


CREATE_BLOCK = (
    "      create:\n"
    "        story:\n"
    "          required: [acceptance_criteria, feature_link]\n"
    "        feature:\n"
    "          required: []\n"
)


def _with_create(block: str) -> str:
    text = config_text()
    assert CREATE_BLOCK in text
    return text.replace(CREATE_BLOCK, block)


@pytest.mark.parametrize(
    ("block", "story", "feature"),
    [
        (CREATE_BLOCK, ("acceptance_criteria", "feature_link"), ()),
        (
            "      create:\n        story:\n          required: [work_started]\n",
            ("work_started",),
            (),
        ),
        (
            "      create:\n        feature:\n          required: [work_started]\n",
            (),
            ("work_started",),
        ),
        ("      create:\n        story: {}\n        feature: {}\n", (), ()),
        ("      create: {}\n", (), ()),
        ("", (), ()),
    ],
)
def test_create_requirements_per_type(
    tmp_path: Path, block: str, story: tuple[str, ...], feature: tuple[str, ...]
) -> None:
    config = parse_config_file(_write(tmp_path, _with_create(block)))
    assert config.jira is not None
    project = config.jira.projects["EXAMPLE"]
    assert project.story_required == story
    assert project.feature_required == feature


@pytest.mark.parametrize(
    ("block", "where", "problem"),
    [
        (
            "      create:\n        required: [acceptance_criteria]\n",
            "jira.projects[0].create",
            "unknown key(s): required",
        ),
        (
            "      create:\n        epic:\n          required: []\n",
            "jira.projects[0].create",
            "unknown key(s): epic",
        ),
        (
            "      create:\n        story:\n          required: []\n          optional: []\n",
            "jira.projects[0].create.story",
            "unknown key(s): optional",
        ),
        ("      create: [story]\n", "jira.projects[0].create", "must be a mapping"),
        (
            "      create:\n        feature: [work_started]\n",
            "jira.projects[0].create.feature",
            "must be a mapping",
        ),
        (
            "      create:\n        story:\n          required: work_started\n",
            "jira.projects[0].create.story.required",
            "must be a list",
        ),
        (
            "      create:\n        feature:\n          required: [work_started, work_started]\n",
            "jira.projects[0].create.feature.required",
            "must not contain duplicates",
        ),
        (
            "      create:\n        story:\n          required: [acceptance_criteria, nope]\n",
            "jira.projects[0].create.story.required[1]",
            "unknown field 'nope'",
        ),
        (
            "      create:\n        feature:\n          required: [nope]\n",
            "jira.projects[0].create.feature.required[0]",
            "unknown field 'nope'",
        ),
        (
            "      create:\n        feature:\n          required: [7]\n",
            "jira.projects[0].create.feature.required[0]",
            "must be a non-empty string",
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
    text = config_text().replace(
        "allowed_issue_types: [Story, Feature]", "allowed_issue_types: &t [Story, Feature]"
    )
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
    text = config_text().replace(
        "{jira_name: Acceptance Criteria, encoding: text}",
        "{id: customfield_10100, encoding: text}",
    )
    config = parse_config_file(_write(tmp_path, text))
    assert config.jira is not None
    assert config.jira.projects["EXAMPLE"].fields["acceptance_criteria"].id == "customfield_10100"
