from __future__ import annotations

import io
import json
import re
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import check_package, release_gate

ROOT = Path(__file__).resolve().parent.parent
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
SHA = "a" * 40
OTHER_SHA = "b" * 40
GOOD_RUN = {
    "head_sha": SHA,
    "head_branch": "main",
    "event": "push",
    "status": "completed",
    "conclusion": "success",
}


@pytest.fixture(scope="module")
def release() -> dict[Any, Any]:
    data = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _steps_using(job: dict[str, Any], action: str) -> list[dict[str, Any]]:
    return [s for s in job["steps"] if str(s.get("uses", "")).startswith(f"{action}@")]


# --------------------------------------------------------------------------- release workflow


def test_release_triggers_only_on_version_tags(release: dict[Any, Any]) -> None:
    # PyYAML reads the bare `on` key as boolean True.
    assert release[True] == {"push": {"tags": ["v*"]}}
    assert release["permissions"] == {"contents": "read"}


def test_release_jobs_run_gate_then_build_then_publish(release: dict[Any, Any]) -> None:
    jobs = release["jobs"]
    assert set(jobs) == {"verify", "build", "publish"}
    assert "needs" not in jobs["verify"]
    assert jobs["build"]["needs"] == "verify"
    assert jobs["publish"]["needs"] == "build"


def test_release_gate_step_checks_provenance(release: dict[Any, Any]) -> None:
    verify = release["jobs"]["verify"]
    assert verify["permissions"] == {"contents": "read", "actions": "read"}
    (checkout,) = _steps_using(verify, "actions/checkout")
    assert checkout["with"] == {"fetch-depth": 0, "persist-credentials": False}
    (gate,) = [s for s in verify["steps"] if "release_gate.py" in s.get("run", "")]
    script = gate["run"]
    assert "git rev-list --first-parent origin/main" in script
    assert "actions/workflows/ci.yml/runs?head_sha=$GITHUB_SHA&branch=main&event=push" in script
    for arg in ('--ref "$GITHUB_REF"', '--sha "$GITHUB_SHA"', "--main-history", "--ci-runs"):
        assert arg in script


def test_release_build_tests_and_checks_distributions(release: dict[Any, Any]) -> None:
    runs = "\n".join(s.get("run", "") for s in release["jobs"]["build"]["steps"])
    for command in (
        "python -m pytest",
        "python -m build",
        "twine check --strict",
        "scripts/check_package.py dist",
    ):
        assert command in runs
    assert "permissions" not in release["jobs"]["build"]


def test_release_publish_uses_scoped_oidc_and_build_artifact(release: dict[Any, Any]) -> None:
    jobs = release["jobs"]
    publish = jobs["publish"]
    assert publish["environment"]["name"] == "pypi"
    assert publish["permissions"] == {"id-token": "write"}
    for name, job in jobs.items():
        if name != "publish":
            assert "id-token" not in job.get("permissions", {}), name
    assert not _steps_using(publish, "actions/checkout")
    (upload,) = _steps_using(jobs["build"], "actions/upload-artifact")
    (download,) = _steps_using(publish, "actions/download-artifact")
    (pypi,) = _steps_using(publish, "pypa/gh-action-pypi-publish")
    assert upload["with"]["name"] == download["with"]["name"] == "dist"
    assert upload["with"]["path"] == download["with"]["path"] == pypi["with"]["packages-dir"]
    assert set(pypi["with"]) == {"packages-dir"}


def test_release_actions_are_sha_pinned_and_use_no_secrets(release: dict[Any, Any]) -> None:
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in text
    for job in release["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step["uses"]


# --------------------------------------------------------------------------- release gate


def test_release_gate_accepts_ci_passing_main_commit() -> None:
    version = release_gate.project_version()
    runs = {"workflow_runs": [GOOD_RUN]}
    assert release_gate.check(f"refs/tags/v{version}", SHA, version, [OTHER_SHA, SHA], runs) == []


@pytest.mark.parametrize(
    "ref",
    ["refs/tags/v9.9.9", "refs/tags/1.0.0", "refs/heads/main", "refs/tags/v1.0.0-rc1", ""],
)
def test_release_gate_rejects_mismatched_tag(ref: str) -> None:
    assert release_gate.tag_problems(ref, "1.0.0")


def test_release_gate_rejects_commit_off_main() -> None:
    assert release_gate.main_history_problems(SHA, [OTHER_SHA])
    assert release_gate.main_history_problems(SHA, [])
    assert release_gate.main_history_problems("abc123", ["abc123"])


@pytest.mark.parametrize(
    "runs",
    [
        {"workflow_runs": []},
        {"workflow_runs": [{**GOOD_RUN, "conclusion": "failure"}]},
        {"workflow_runs": [{**GOOD_RUN, "status": "in_progress", "conclusion": None}]},
        {"workflow_runs": [{**GOOD_RUN, "head_sha": OTHER_SHA}]},
        {"workflow_runs": [{**GOOD_RUN, "head_branch": "feature"}]},
        {"workflow_runs": [{**GOOD_RUN, "event": "pull_request"}]},
        {"message": "Not Found"},
        [],
    ],
)
def test_release_gate_rejects_missing_or_failed_ci(runs: Any) -> None:
    assert release_gate.ci_problems(SHA, runs)


def test_release_gate_cli_fails_closed(tmp_path: Path) -> None:
    history = tmp_path / "history.txt"
    history.write_text(f"{SHA}\n", encoding="utf-8")
    runs = tmp_path / "runs.json"
    runs.write_text(json.dumps({"workflow_runs": [GOOD_RUN]}), encoding="utf-8")
    ref = f"refs/tags/v{release_gate.project_version()}"
    args = ["--ref", ref, "--sha", SHA, "--main-history", str(history)]
    assert release_gate.main([*args, "--ci-runs", str(runs)]) == 0
    assert release_gate.main([*args, "--ci-runs", str(tmp_path / "missing.json")]) == 1
    runs.write_text("not json", encoding="utf-8")
    assert release_gate.main([*args, "--ci-runs", str(runs)]) == 1
    args[1] = "refs/tags/v0.0.0"
    runs.write_text(json.dumps({"workflow_runs": [GOOD_RUN]}), encoding="utf-8")
    assert release_gate.main([*args, "--ci-runs", str(runs)]) == 1


# --------------------------------------------------------------------------- package license

LICENSE_TEXT = (ROOT / "LICENSE").read_text(encoding="utf-8")
METADATA = "\n".join(
    ["Metadata-Version: 2.4", "Name: atlass-cli", *check_package.EXPECTED_METADATA, "", "body"]
)


def test_source_license_is_the_complete_mit_text() -> None:
    assert check_package.license_problems(LICENSE_TEXT) == []


@pytest.mark.parametrize("clause", sorted(check_package.LICENSE_CLAUSES))
def test_truncated_license_fails(clause: str) -> None:
    first_words = " ".join(check_package.LICENSE_CLAUSES[clause].split()[:4])
    paragraphs = [p for p in LICENSE_TEXT.split("\n\n") if first_words not in " ".join(p.split())]
    problems = check_package.license_problems("\n\n".join(paragraphs))
    assert f"LICENSE is missing the MIT {clause}" in problems


def test_title_only_license_fails() -> None:
    assert len(check_package.license_problems("MIT License\n")) == 4


def _write_dists(dist: Path, license_bytes: bytes, metadata: str = METADATA) -> None:
    dist.mkdir()
    with zipfile.ZipFile(dist / "atlass_cli-1.0.0-py3-none-any.whl", "w") as zf:
        zf.writestr("atlass_cli-1.0.0.dist-info/METADATA", metadata)
        zf.writestr("atlass_cli-1.0.0.dist-info/licenses/LICENSE", license_bytes)
    with tarfile.open(dist / "atlass_cli-1.0.0.tar.gz", "w:gz") as tf:
        for name, data in (("PKG-INFO", metadata.encode()), ("LICENSE", license_bytes)):
            info = tarfile.TarInfo(f"atlass_cli-1.0.0/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def test_package_check_accepts_matching_archives(tmp_path: Path) -> None:
    _write_dists(tmp_path / "dist", (ROOT / "LICENSE").read_bytes())
    assert check_package.check(tmp_path / "dist") == []


def test_package_check_rejects_truncated_archive_license(tmp_path: Path) -> None:
    truncated = LICENSE_TEXT.split("THE SOFTWARE IS PROVIDED")[0].encode()
    _write_dists(tmp_path / "dist", truncated)
    problems = check_package.check(tmp_path / "dist")
    assert "wheel LICENSE differs from the source LICENSE" in problems
    assert "sdist LICENSE differs from the source LICENSE" in problems


def test_package_check_rejects_truncated_source_license(tmp_path: Path) -> None:
    truncated = LICENSE_TEXT.split("THE SOFTWARE IS PROVIDED")[0].encode()
    source = tmp_path / "LICENSE"
    source.write_bytes(truncated)
    _write_dists(tmp_path / "dist", truncated)
    assert check_package.check(tmp_path / "dist", source) == [
        "LICENSE is missing the MIT disclaimer"
    ]


def test_package_check_rejects_bad_metadata_and_missing_archives(tmp_path: Path) -> None:
    legacy = METADATA.replace("License-Expression: MIT", "License: MIT")
    _write_dists(tmp_path / "dist", (ROOT / "LICENSE").read_bytes(), legacy)
    problems = check_package.check(tmp_path / "dist")
    assert "wheel metadata is missing 'License-Expression: MIT'" in problems
    assert "sdist metadata has a legacy License field" in problems
    (tmp_path / "empty").mkdir()
    assert check_package.check(tmp_path / "empty")
