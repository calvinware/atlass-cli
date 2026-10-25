"""Release provenance gate for .github/workflows/release.yml. Every check fails closed.

A release tag is accepted only when all of these hold:

- the ref is exactly `refs/tags/v<project.version>` from pyproject.toml;
- the tagged commit is on the first-parent history of `main`, so it is a reviewed merge commit;
- a `push` run of the CI workflow on `main` for that exact commit concluded `success`.

The workflow collects the inputs (no network access happens here):

    git rev-list --first-parent origin/main > main-history.txt
    gh api 'repos/OWNER/REPO/actions/workflows/ci.yml/runs?head_sha=SHA&branch=main&event=push' \\
        > ci-runs.json
    python scripts/release_gate.py --ref "$GITHUB_REF" --sha "$GITHUB_SHA" \\
        --main-history main-history.txt --ci-runs ci-runs.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SHA_RE = re.compile(r"[0-9a-f]{40}")


def project_version(pyproject: Path = ROOT / "pyproject.toml") -> str:
    with pyproject.open("rb") as f:
        return str(tomllib.load(f)["project"]["version"])


def tag_problems(ref: str, version: str) -> list[str]:
    expected = f"refs/tags/v{version}"
    if ref != expected:
        return [f"{ref!r} does not match project.version; expected {expected!r}"]
    return []


def main_history_problems(sha: str, main_history: Iterable[str]) -> list[str]:
    if not SHA_RE.fullmatch(sha):
        return [f"{sha!r} is not a full commit SHA"]
    if sha not in {line.strip() for line in main_history}:
        return [f"{sha} is not a commit on the first-parent history of main"]
    return []


def ci_problems(sha: str, runs: Any) -> list[str]:
    if not isinstance(runs, dict) or not isinstance(runs.get("workflow_runs"), list):
        return ["CI run listing is not a GitHub workflow runs response"]
    passed = [
        run
        for run in runs["workflow_runs"]
        if isinstance(run, dict)
        and run.get("head_sha") == sha
        and run.get("head_branch") == "main"
        and run.get("event") == "push"
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
    ]
    if not passed:
        return [f"no successful CI push run on main for {sha}; wait for CI on main to pass"]
    return []


def check(ref: str, sha: str, version: str, main_history: Iterable[str], runs: Any) -> list[str]:
    return [
        *tag_problems(ref, version),
        *main_history_problems(sha, main_history),
        *ci_problems(sha, runs),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--main-history", type=Path, required=True)
    parser.add_argument("--ci-runs", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        history = args.main_history.read_text(encoding="utf-8").splitlines()
        runs = json.loads(args.ci_runs.read_text(encoding="utf-8"))
        problems = check(args.ref, args.sha, project_version(), history, runs)
    except (OSError, ValueError, KeyError) as exc:
        problems = [f"release gate inputs are unreadable: {exc}"]
    if problems:
        sys.stderr.write("".join(f"::error::{p}\n" for p in problems))
        return 1
    sys.stdout.write(f"release gate passed for {args.ref} at {args.sha}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
