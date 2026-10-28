"""`yojo init`: live-checked drafts, explicit policy, and conflict-safe saves."""

from __future__ import annotations

import json
import os
import shlex
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.conftest import (
    BITBUCKET,
    BITBUCKET_TOKEN,
    CONFLUENCE,
    CONFLUENCE_TOKEN,
    JIRA,
    JIRA_TOKEN,
    Harness,
    backticked,
    bitbucket_path,
    confluence_path,
    fixture,
    jira_path,
)
from yojo import files
from yojo.config import load_config, parse_config_file, validate_config

MYSELF = jira_path("/rest/api/2/myself")
PROJECT = jira_path("/rest/api/2/project/PROJ")
STATUSES = jira_path("/rest/api/2/project/PROJ/statuses")
FIELDS = jira_path("/rest/api/2/field")
CONFLUENCE_ME = confluence_path("/rest/api/user/current")
PROPS = bitbucket_path("/application-properties")
RECENT = bitbucket_path("/profile/recent/repos")
REPO = bitbucket_path("/projects/PROJ/repos/web")
INIT = ["init", "--jira-url", JIRA + "/", "--project", "PROJ"]
TOKENS = (JIRA_TOKEN, CONFLUENCE_TOKEN, BITBUCKET_TOKEN)

POLICY = {
    "jira": {
        "fields": {"points": {"jira_name": "story points"}},
        "projects": [
            {
                "key": "PROJ",
                "create": {"Bug": {"required": ["Severity"]}},
                "transitions": {
                    "finish": {
                        "from": ["In Progress"],
                        "to": "Done",
                        "require": [{"kind": "field_set", "field": "points"}],
                    }
                },
            }
        ],
    }
}


def jira_live(harness: Harness) -> None:
    harness.store()
    harness.server.add("GET", MYSELF, {"name": "example-user"})
    harness.server.add("GET", PROJECT, fixture("jira_project.json"))


def all_live(harness: Harness) -> None:
    jira_live(harness)
    harness.store("confluence", CONFLUENCE_TOKEN)
    harness.store("bitbucket", BITBUCKET_TOKEN)
    harness.server.add("GET", CONFLUENCE_ME, {"username": "example-user", "type": "known"})
    harness.server.add("GET", PROPS, fixture("bitbucket_application_properties.json"))
    harness.server.add("GET", RECENT, {"values": [], "isLastPage": True})
    harness.server.add("GET", REPO, fixture("bitbucket_repo.json"))
    default_branch = fixture("bitbucket_branches.json")["values"][0]
    harness.server.add("GET", REPO + "/branches/default", default_branch)


def policy_live(harness: Harness) -> None:
    jira_live(harness)
    harness.server.add("GET", FIELDS, fixture("fields.json"))
    statuses = [
        {"name": "Bug", "statuses": [{"name": n} for n in ("To Do", "In Progress", "Done")]}
    ]
    harness.server.add("GET", STATUSES, statuses)
    harness.server.add(
        "GET",
        jira_path("/rest/api/2/issue/createmeta/PROJ/issuetypes"),
        fixture("createmeta_issuetypes.json"),
    )


def no_tokens(*texts: str) -> None:
    for text in texts:
        for token in TOKENS:
            assert token not in text


def leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.startswith("."))


# --------------------------------------------------------------------------- preview


def test_preview_drafts_only_what_was_named(harness: Harness) -> None:
    jira_live(harness)
    result = harness.run(*INIT)
    assert result.code == 0, result.out
    data = result.data
    assert data["action"] == "preview"
    assert data["existing_sha256"] is None
    assert data["path"] == str(harness.tmp_path / "yojo.yaml")
    doc = yaml.safe_load(data["yaml"])
    assert doc == {"version": 1, "jira": {"base_url": JIRA, "projects": [{"key": "PROJ"}]}}
    validate_config(doc, harness.tmp_path / "yojo.yaml")
    assert data["sha256"] == files.sha256(data["yaml"].encode())
    assert data["services"] == [
        {"service": "jira", "base_url": JIRA, "user": "example-user", "version": None}
    ]
    assert data["projects"] == [{"key": "PROJ", "name": "Example Project"}]
    assert data["next"] == [f"yojo --json init --jira-url {JIRA} --project PROJ --save"]
    assert data["content_trust"] == "untrusted"
    assert not (harness.tmp_path / "yojo.yaml").exists()
    assert harness.server.writes() == []
    no_tokens(result.stdout)


def test_custom_ca_bundle_is_used_for_live_checks_and_saved(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    jira_live(harness)
    bundle = harness.tmp_path / "team-ca.pem"
    bundle.write_text("example CA fixture")
    from yojo import cli

    original_client = cli._client
    observed = []

    def recording_client(rt: Any, service: str, cfg: Any) -> Any:
        observed.append((service, cfg.ca_bundle))
        return original_client(rt, service, cfg)

    monkeypatch.setattr(cli, "_client", recording_client)
    result = harness.run(*INIT, "--ca-bundle", "team-ca.pem", "--save")
    assert result.code == 0, result.out
    assert observed == [("jira", bundle)]
    assert yaml.safe_load(result.data["yaml"])["jira"]["ca_bundle"] == "team-ca.pem"
    saved_config = parse_config_file(harness.tmp_path / "yojo.yaml")
    assert saved_config.jira is not None
    assert saved_config.jira.ca_bundle == bundle
    assert result.data["action"] == "created"


def test_missing_custom_ca_bundle_fails_before_network_or_save(harness: Harness) -> None:
    result = harness.run(*INIT, "--ca-bundle", "missing.pem", "--save")
    assert result.error["code"] == "input_invalid"
    assert "--ca-bundle" in result.error["fix"]
    assert harness.server.requests == []
    assert not (harness.tmp_path / "yojo.yaml").exists()


def test_custom_ca_missing_login_fix_keeps_the_ca_option(harness: Harness) -> None:
    bundle = harness.tmp_path / "team-ca.pem"
    bundle.write_text("example CA fixture")
    result = harness.run(*INIT, "--ca-bundle", "team-ca.pem")
    assert result.error["code"] == "auth_missing"
    login = backticked(result.error["fix"], "yojo --json login")
    assert len(login) == 1
    args = shlex.split(login[0])
    assert args[:5] == ["yojo", "--json", "login", "--service", "jira"]
    assert args[args.index("--ca-bundle") + 1] == str(bundle)


def test_custom_ca_login_fix_names_the_bundle_init_validated(harness: Harness) -> None:
    bundle = harness.tmp_path / "config" / "certs" / "team-ca.pem"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("example CA fixture")
    argv = [*INIT, "--ca-bundle", "certs/team-ca.pem", "--output", "config/yojo.yaml"]
    result = harness.run(*argv)
    assert result.error["code"] == "auth_missing"
    login = backticked(result.error["fix"], "yojo --json login")
    assert len(login) == 1
    args = shlex.split(login[0])
    assert args[args.index("--ca-bundle") + 1] == str(bundle)
    assert not (harness.tmp_path / "certs").exists()
    assert harness.server.requests == []


def test_preview_checks_every_service_and_reports_repos_without_saving_them(
    harness: Harness,
) -> None:
    all_live(harness)
    result = harness.run(
        *INIT,
        "--confluence-url",
        CONFLUENCE,
        "--bitbucket-url",
        BITBUCKET,
        "--repo",
        "PROJ/web",
        "--repo",
        "PROJ/web",
    )
    assert result.code == 0, result.out
    data = result.data
    assert [s["service"] for s in data["services"]] == ["jira", "confluence", "bitbucket"]
    assert data["services"][2]["version"] == "8.19.1"
    assert data["repos"] == [{"repo": "PROJ/web", "name": "web"}]
    doc = yaml.safe_load(data["yaml"])
    assert doc["confluence"] == {"base_url": CONFLUENCE}
    assert doc["bitbucket"] == {"base_url": BITBUCKET}
    assert "PROJ/web" not in data["yaml"]
    assert any("not saved" in note for note in data["notes"])
    assert harness.server.paths().count(RECENT) == 1


def test_missing_login_gives_the_login_fix_and_writes_nothing(harness: Harness) -> None:
    result = harness.run(*INIT, "--save")
    assert result.code == 3
    assert result.error["code"] == "auth_missing"
    assert f"yojo login --service jira --base-url {JIRA}" in result.error["fix"]
    assert harness.server.requests == []
    assert not (harness.tmp_path / "yojo.yaml").exists()


def test_unknown_project_fails_closed(harness: Harness) -> None:
    harness.store()
    harness.server.add("GET", MYSELF, {"name": "example-user"})
    harness.server.add("GET", PROJECT, {"errorMessages": ["No project"]}, status=404)
    result = harness.run(*INIT, "--save")
    assert result.code == 5
    assert result.error["code"] == "not_found"
    assert "--project PROJ" in result.error["fix"]
    assert not (harness.tmp_path / "yojo.yaml").exists()


@pytest.mark.parametrize(
    "body", [{"key": "OTHER", "name": "x"}, {"key": "PROJ"}, {"key": "PROJ", "name": ""}, []]
)
def test_malformed_project_metadata_fails_closed(harness: Harness, body: Any) -> None:
    harness.store()
    harness.server.add("GET", MYSELF, {"name": "example-user"})
    harness.server.add("GET", PROJECT, body)
    result = harness.run(*INIT, "--save")
    assert result.error["code"] == "response_invalid"
    assert not (harness.tmp_path / "yojo.yaml").exists()


def test_unknown_repository_fails_closed(harness: Harness) -> None:
    harness.store("bitbucket", BITBUCKET_TOKEN)
    harness.server.add("GET", PROPS, fixture("bitbucket_application_properties.json"))
    harness.server.add("GET", RECENT, {"values": [], "isLastPage": True})
    harness.server.add("GET", REPO, {"errors": [{"message": "No repo"}]}, status=404)
    result = harness.run("init", "--bitbucket-url", BITBUCKET, "--repo", "PROJ/web", "--save")
    assert result.error["code"] == "not_found"
    assert "--repo PROJ/web" in result.error["fix"]
    assert not (harness.tmp_path / "yojo.yaml").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["init"],
        ["init", "--project", "PROJ"],
        ["init", "--jira-url", JIRA, "--repo", "PROJ/web"],
        ["init", "--jira-url", "http://jira.example.test/jira"],
        ["init", "--jira-url", "https://user:pw@jira.example.test/jira"],
        ["init", "--jira-url", JIRA, "--replace"],
        ["init", "--jira-url", JIRA, "--save", "--replace"],
        ["init", "--jira-url", JIRA, "--expected-sha256", "0" * 64],
        ["init", "--jira-url", JIRA, "--expected-sha256", "not-hex"],
        ["init", "--confluence-url", CONFLUENCE, "--input", "-"],
        ["--base-url", JIRA, "init", "--jira-url", JIRA],
        ["--config", "other.yaml", "init", "--jira-url", JIRA],
    ],
)
def test_flag_mistakes_are_refused_before_any_call(harness: Harness, argv: list[str]) -> None:
    result = harness.run(*argv)
    assert result.code == 2
    assert result.error["code"] == "usage_error"
    assert result.error["fix"]
    assert harness.server.requests == []
    assert "pw@" not in result.stdout


# --------------------------------------------------------------------------- policy


@pytest.mark.parametrize("flag", ["--policy-input", "--input"])
def test_explicit_policy_is_checked_live_and_merged(harness: Harness, flag: str) -> None:
    policy_live(harness)
    result = harness.run(*INIT, flag, "-", stdin=json.dumps(POLICY).encode())
    assert result.code == 0, result.out
    doc = yaml.safe_load(result.data["yaml"])
    assert doc["jira"]["fields"] == POLICY["jira"]["fields"]
    assert doc["jira"]["projects"] == POLICY["jira"]["projects"]
    assert result.data["next"][0].endswith("--input - --save")
    assert not any("No team aliases" in note for note in result.data["notes"])
    assert harness.server.writes() == []


def boundary_policy(auto_set_value: str) -> dict[str, Any]:
    policy: dict[str, Any] = json.loads(json.dumps(POLICY))
    project = policy["jira"]["projects"][0]
    project["create"]["Task"] = {"required": []}
    project["transitions"]["finish"]["auto_set"] = {"Description": auto_set_value}
    return policy


def test_policy_accepts_loader_limits_through_preview_save_and_reload(
    harness: Harness,
) -> None:
    policy_live(harness)
    policy = boundary_policy("x" * 32767)
    stdin = json.dumps(policy).encode()
    preview = harness.run(*INIT, "--input", "-", stdin=stdin)
    assert preview.code == 0, preview.out
    saved = harness.run(*INIT, "--input", "-", "--save", stdin=stdin)
    assert saved.code == 0, saved.out
    assert saved.data["action"] == "created"
    config = load_config(None, harness.tmp_path)
    assert config is not None
    assert config.jira is not None
    project = config.jira.projects["PROJ"]
    assert project.create_required["Task"] == ()
    assert project.transitions["finish"].auto_set["Description"] == "x" * 32767


def test_policy_refuses_auto_set_value_over_loader_limit(harness: Harness) -> None:
    policy = boundary_policy("x" * 32768)
    result = harness.run(*INIT, "--input", "-", "--save", stdin=json.dumps(policy).encode())
    assert result.code == 2
    assert result.error["code"] == "input_invalid"
    assert "input schema" in result.error["message"]
    assert not (harness.tmp_path / "yojo.yaml").exists()
    assert harness.server.requests == []


def test_policy_names_that_jira_does_not_have_are_refused(harness: Harness) -> None:
    policy_live(harness)
    policy = json.loads(json.dumps(POLICY))
    policy["jira"]["fields"]["points"]["jira_name"] = "Story Pints"
    policy["jira"]["projects"][0]["transitions"]["finish"]["to"] = "Dnoe"
    result = harness.run(*INIT, "--save", "--input", "-", stdin=json.dumps(policy).encode())
    assert result.code == 2
    error = result.error
    assert error["code"] == "input_invalid"
    problems = {p["id"]: p for p in error["details"]["problems"]}
    assert problems["field:points"]["details"]["suggestion"] == "Story Points"
    assert problems["PROJ:transition:finish:to"]["details"]["suggestion"] == "Done"
    assert not (harness.tmp_path / "yojo.yaml").exists()


def test_transition_policy_refuses_missing_status_metadata(harness: Harness) -> None:
    jira_live(harness)
    harness.server.add("GET", FIELDS, fixture("fields.json"))
    harness.server.add("GET", STATUSES, [])
    policy = {
        "jira": {
            "projects": [
                {
                    "key": "PROJ",
                    "transitions": {"finish": {"from": ["In Progress"], "to": "Done"}},
                }
            ]
        }
    }
    result = harness.run(*INIT, "--save", "--input", "-", stdin=json.dumps(policy).encode())
    assert result.error["code"] == "input_invalid"
    assert any(p["id"] == "PROJ:statuses" for p in result.error["details"]["problems"])
    assert not (harness.tmp_path / "yojo.yaml").exists()


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ({"jira": {"projects": [{"key": "OTHER"}]}}, "not a --project"),
        ({"jira": {"projects": [{"key": "PROJ"}, {"key": "PROJ"}]}}, "twice"),
        ({"jira": {"projects": [{"key": "PROJ", "owner": "x"}]}}, "input schema"),
        ({"jira": {"fields": {"Bad Name": {"id": "summary"}}}}, "input schema"),
        ({"jira": {"fields": {"pts": {"jira_name": "A", "id": "summary"}}}}, "input schema"),
        ({"confluence": {}}, "input schema"),
        (
            {
                "jira": {
                    "projects": [{"key": "PROJ", "transitions": {"t": {"from": [], "to": "D"}}}]
                }
            },
            "input schema",
        ),
        (
            {
                "jira": {
                    "projects": [
                        {
                            "key": "PROJ",
                            "transitions": {
                                "a": {"from": ["To Do"], "to": "Done"},
                                "b": {"from": ["To Do"], "to": "done"},
                            },
                        }
                    ]
                }
            },
            "would not be a valid yojo.yaml",
        ),
    ],
)
def test_bad_policy_documents_are_refused_offline(
    harness: Harness, policy: dict[str, Any], expected: str
) -> None:
    result = harness.run(*INIT, "--input", "-", stdin=json.dumps(policy).encode())
    assert result.code == 2
    assert result.error["code"] == "input_invalid"
    assert expected in result.error["message"]
    assert harness.server.requests == []


def test_oversized_draft_is_refused_before_create_or_replace(harness: Harness) -> None:
    policy = {"jira": {"fields": {f"field_{i:05d}": {"id": "summary"} for i in range(8000)}}}
    input_bytes = json.dumps(policy).encode()
    path = harness.tmp_path / "yojo.yaml"
    create = harness.run("init", "--jira-url", JIRA, "--input", "-", "--save", stdin=input_bytes)
    assert create.error["code"] == "input_invalid"
    assert "config limit" in create.error["message"]
    assert not path.exists()

    original = f"version: 1\njira:\n  base_url: {JIRA}\n".encode()
    path.write_bytes(original)
    replace = harness.run(
        "init", "--jira-url", JIRA, "--input", "-", "--save", "--replace",
        "--expected-sha256", files.sha256(original), stdin=input_bytes,
    )
    assert replace.error["code"] == "input_invalid"
    assert path.read_bytes() == original
    assert harness.server.requests == []


# --------------------------------------------------------------------------- save


def test_save_creates_then_repeats_without_writing(harness: Harness) -> None:
    jira_live(harness)
    first = harness.run(*INIT, "--save")
    assert first.code == 0, first.out
    assert first.data["action"] == "created"
    path = harness.tmp_path / "yojo.yaml"
    saved = path.read_bytes()
    assert saved.decode("utf-8") == first.data["yaml"]
    assert first.data["next"][:2] == [
        "yojo --json config check",
        "yojo --json agent install --target agents",
    ]
    assert "jira search" in first.data["next"][2]
    config = load_config(None, harness.tmp_path)
    assert config is not None and config.jira is not None
    assert config.jira.base_url == JIRA
    before = path.stat().st_mtime_ns
    second = harness.run(*INIT, "--save")
    assert second.data["action"] == "unchanged"
    assert second.data["existing_sha256"] == first.data["sha256"]
    assert path.read_bytes() == saved
    assert path.stat().st_mtime_ns == before
    assert leftovers(harness.tmp_path) == []
    no_tokens(saved.decode(), first.stdout, second.stdout)


def test_output_path_is_relative_to_the_current_directory(harness: Harness) -> None:
    jira_live(harness)
    result = harness.run(*INIT, "--save", "--output", "config/team.yaml")
    assert result.data["action"] == "created"
    saved = harness.tmp_path / "config" / "team.yaml"
    parse_config_file(saved)
    check, install, read = result.data["next"]
    assert shlex.split(check) == ["yojo", "--json", "config", "check", "--config", str(saved)]
    assert install == "yojo --json agent install --target agents"
    assert shlex.split(read)[-4:] == ["--limit", "5", "--config", str(saved)]


def test_next_steps_after_a_custom_output_run_from_the_same_directory(harness: Harness) -> None:
    jira_live(harness)
    harness.server.add("GET", FIELDS, fixture("fields.json"))
    harness.server.add("GET", STATUSES, [{"name": "Bug", "statuses": [{"name": "To Do"}]}])
    row = {"id": "1", "key": "PROJ-1", "fields": {"summary": "a", "status": {"name": "To Do"}}}
    search = {"startAt": 0, "maxResults": 5, "total": 1, "issues": [row]}
    harness.server.add("GET", jira_path("/rest/api/2/search"), search)
    result = harness.run(*INIT, "--save", "--output", "config/team.yaml")
    assert result.data["action"] == "created"
    assert not (harness.tmp_path / "yojo.yaml").exists()
    for step in result.data["next"]:
        argv = shlex.split(step)
        assert argv[:2] == ["yojo", "--json"]
        ran = harness.run(*argv[1:])
        assert ran.code == 0, (step, ran.out)
    assert (harness.tmp_path / "AGENTS.md").is_file()
    assert harness.server.paths("GET")[-1] == jira_path("/rest/api/2/search")
    assert harness.server.writes() == []


def test_save_never_overwrites_a_different_file(harness: Harness) -> None:
    jira_live(harness)
    path = harness.tmp_path / "yojo.yaml"
    path.write_bytes(b"version: 1\nconfluence:\n  base_url: https://confluence.example.test\n")
    original = path.read_bytes()
    result = harness.run(*INIT, "--save")
    assert result.code == 2
    error = result.error
    assert error["code"] == "file_conflict"
    digest = files.sha256(original)
    assert error["details"]["existing_sha256"] == digest
    assert f"--save --replace --expected-sha256 {digest}" in error["fix"]
    assert path.read_bytes() == original


def test_preview_of_an_existing_file_offers_the_guarded_replace(harness: Harness) -> None:
    jira_live(harness)
    path = harness.tmp_path / "yojo.yaml"
    path.write_text("version: 1\nconfluence:\n  base_url: https://confluence.example.test\n")
    preview = harness.run(*INIT)
    digest = preview.data["existing_sha256"]
    assert digest == files.sha256(path.read_bytes())
    assert preview.data["next"] == [
        f"yojo --json init --jira-url {JIRA} --project PROJ --save --replace "
        f"--expected-sha256 {digest}"
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_replace_with_the_previewed_digest_keeps_permissions(harness: Harness) -> None:
    jira_live(harness)
    path = harness.tmp_path / "yojo.yaml"
    path.write_text("version: 1\nconfluence:\n  base_url: https://confluence.example.test\n")
    path.chmod(0o640)
    digest = harness.run(*INIT).data["existing_sha256"]
    result = harness.run(*INIT, "--save", "--replace", "--expected-sha256", digest.upper())
    assert result.code == 0, result.out
    assert result.data["action"] == "replaced"
    assert path.read_text() == result.data["yaml"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    parse_config_file(path)
    assert leftovers(harness.tmp_path) == []


def test_replace_refuses_a_file_that_changed_since_the_preview(harness: Harness) -> None:
    jira_live(harness)
    path = harness.tmp_path / "yojo.yaml"
    path.write_text("version: 1\nconfluence:\n  base_url: https://confluence.example.test\n")
    digest = harness.run(*INIT).data["existing_sha256"]
    path.write_text("version: 1\nbitbucket:\n  base_url: https://bitbucket.example.test\n")
    changed = path.read_bytes()
    result = harness.run(*INIT, "--save", "--replace", "--expected-sha256", digest)
    assert result.error["code"] == "file_conflict"
    assert "changed since the preview" in result.error["message"]
    assert path.read_bytes() == changed


def test_replace_checks_digest_even_when_draft_matches_current_file(harness: Harness) -> None:
    jira_live(harness)
    path = harness.tmp_path / "yojo.yaml"
    saved = harness.run(*INIT, "--save")
    assert saved.data["action"] == "created"
    original = path.read_bytes()
    result = harness.run(*INIT, "--save", "--replace", "--expected-sha256", "0" * 64)
    assert result.error["code"] == "file_conflict"
    assert result.error["details"]["existing_sha256"] == files.sha256(original)
    assert path.read_bytes() == original


def test_replace_needs_an_existing_file(harness: Harness) -> None:
    jira_live(harness)
    result = harness.run(*INIT, "--save", "--replace", "--expected-sha256", "0" * 64)
    assert result.error["code"] == "file_conflict"
    assert not (harness.tmp_path / "yojo.yaml").exists()


def test_a_file_that_appears_mid_save_is_not_overwritten(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    jira_live(harness)
    path = harness.tmp_path / "yojo.yaml"
    real_stage = files._stage

    def racing_stage(dest: Path, data: bytes, mode: int) -> Path:
        staged = real_stage(dest, data, mode)
        dest.write_text("written by someone else\n")
        return staged

    monkeypatch.setattr(files, "_stage", racing_stage)
    result = harness.run(*INIT, "--save")
    assert result.error["code"] == "file_conflict"
    assert path.read_text() == "written by someone else\n"
    assert leftovers(harness.tmp_path) == []


def test_external_deletion_during_replace_reports_conflict(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    jira_live(harness)
    path = harness.tmp_path / "yojo.yaml"
    path.write_text("version: 1\nconfluence:\n  base_url: https://confluence.example.test\n")
    digest = files.sha256(path.read_bytes())
    real_publish = files._publish_locked

    def racing_publish(dest: Path, data: bytes, expected: bytes | None) -> None:
        dest.unlink()
        real_publish(dest, data, expected)

    monkeypatch.setattr(files, "_publish_locked", racing_publish)
    result = harness.run(*INIT, "--save", "--replace", "--expected-sha256", digest)
    assert result.error["code"] == "file_conflict"
    assert not path.exists()
    assert leftovers(harness.tmp_path) == []


def test_an_interrupted_temp_write_leaves_the_original(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    jira_live(harness)
    path = harness.tmp_path / "yojo.yaml"
    path.write_text("version: 1\nconfluence:\n  base_url: https://confluence.example.test\n")
    original = path.read_bytes()
    digest = files.sha256(original)

    def broken_fsync(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", broken_fsync)
    result = harness.run(*INIT, "--save", "--replace", "--expected-sha256", digest)
    assert result.error["code"] == "file_invalid"
    assert path.read_bytes() == original
    assert leftovers(harness.tmp_path) == []


def test_another_yojo_writer_holds_the_lock(harness: Harness) -> None:
    jira_live(harness)
    lock = harness.tmp_path / ".yojo.yaml.yojo-lock"
    lock.write_text("")
    result = harness.run(*INIT, "--save")
    assert result.error["code"] == "file_conflict"
    assert str(lock) in result.error["fix"]
    assert not (harness.tmp_path / "yojo.yaml").exists()
    assert lock.exists()


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError:
        pytest.skip("symbolic links are not available")


def test_symlinked_destination_is_refused(harness: Harness) -> None:
    jira_live(harness)
    real = harness.tmp_path / "real.yaml"
    real.write_text("keep\n")
    _symlink(harness.tmp_path / "yojo.yaml", real)
    for extra in ([], ["--save"]):
        result = harness.run(*INIT, *extra)
        assert result.error["code"] == "file_invalid"
    assert real.read_text() == "keep\n"


def test_symlinked_parent_is_refused(harness: Harness) -> None:
    jira_live(harness)
    real = harness.tmp_path / "real"
    real.mkdir()
    _symlink(harness.tmp_path / "linked", real)
    result = harness.run(*INIT, "--save", "--output", "linked/yojo.yaml")
    assert result.error["code"] == "file_invalid"
    assert list(real.iterdir()) == []


def test_symlinked_ancestor_of_absolute_output_outside_cwd_is_refused(
    harness: Harness,
) -> None:
    jira_live(harness)
    root = harness.tmp_path
    real = root / "real"
    real.mkdir()
    _symlink(root / "linked", real)
    cwd = root / "cwd"
    cwd.mkdir()
    harness.tmp_path = cwd
    result = harness.run(*INIT, "--save", "--output", str(root / "linked" / "yojo.yaml"))
    assert result.error["code"] == "file_invalid"
    assert list(real.iterdir()) == []


def test_a_lone_legacy_file_is_not_migrated(harness: Harness) -> None:
    jira_live(harness)
    (harness.tmp_path / "atl.yaml").write_text("version: 1\n")
    result = harness.run(*INIT, "--save")
    assert result.error["code"] == "config_legacy"
    assert not (harness.tmp_path / "yojo.yaml").exists()


def test_terminal_view_shows_the_draft_and_next_step(harness: Harness) -> None:
    jira_live(harness)
    view = harness.run_tty(*INIT, environ={"NO_COLOR": "1"})
    assert view.code == 0
    assert view.stdout.startswith("Draft yojo.yaml (not saved) for ")
    assert "base_url: https://jira.example.test/jira" in view.stdout
    assert "Next" in view.stdout and "--save" in view.stdout
    assert "\x1b" not in view.stdout
