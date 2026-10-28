"""Checks on the hand-written pages: images, neutral hosts, the README's first screen, the setup
gallery, agent install lines, and the Pages workflow. No network is used."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from yojo.agents import TARGETS

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
README = ROOT / "README.md"
PAGES_WORKFLOW = ROOT / ".github" / "workflows" / "pages.yml"
SHARED_MARKDOWN = [README, ROOT / "AGENTS.md", *sorted(DOCS.rglob("*.md"))]
RAW = re.search(  # the one pinned revision README images load from
    r"https://raw\.githubusercontent\.com/calvinware/yojo/[0-9a-f]{40}/",
    README.read_text(encoding="utf-8"),
).group(0)  # type: ignore[union-attr]

MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?[^)]*\)")
HTML_IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
ALT_RE = re.compile(r'\balt="([^"]*)"', re.I)
HOST_RE = re.compile(r"https?://([^/\s)\"'`>|\\]+)")
# Public hosts the docs may name. Everything else must be a synthetic example host.
ALLOWED_HOSTS = frozenset(
    {
        "github.com",
        "raw.githubusercontent.com",
        "img.shields.io",
        "pypi.org",
        "docs.pypi.org",
        "pipx.pypa.io",
        "docs.astral.sh",
        "example.com",
    }
)
SETUP_STEPS = 7


def _ids(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _images(text: str) -> list[tuple[str, str]]:
    """(alt, source) for every Markdown and HTML image."""
    found = [(alt.strip(), src) for alt, src in MD_IMAGE_RE.findall(text)]
    for tag in HTML_IMG_RE.findall(text):
        alt = ALT_RE.search(tag)
        src = re.search(r'\bsrc="([^"]*)"', tag)
        found.append(((alt.group(1).strip() if alt else ""), src.group(1) if src else ""))
    return found


def _host_problems(text: str) -> list[str]:
    problems = []
    for raw in HOST_RE.findall(text):
        if not re.search(r"[a-z]", raw):
            continue  # a placeholder such as https://... or https://HOST
        host = raw.lower().split(":", 1)[0]
        if host in ALLOWED_HOSTS or host.endswith(".example.test"):
            continue
        problems.append(host)
    return problems


# --------------------------------------------------------------------------- images


@pytest.mark.parametrize("path", SHARED_MARKDOWN, ids=_ids)
def test_every_image_has_alt_text(path: Path) -> None:
    for alt, src in _images(path.read_text(encoding="utf-8")):
        # A status badge's alt is its label; every other image says what it shows.
        badge = src.startswith("https://img.shields.io/") or src.endswith("/badge.svg")
        assert len(alt) >= (2 if badge else 10), f"{src!r} needs alt text that says what it shows"


def test_image_checks_catch_missing_alt() -> None:
    assert _images("![](a.svg)") == [("", "a.svg")]
    assert _images('<img src="a.gif">') == [("", "a.gif")]
    assert _images('<img alt="A terminal recording" src="a.gif">')[0][0] == "A terminal recording"


@pytest.mark.parametrize("path", SHARED_MARKDOWN, ids=_ids)
def test_theme_images_come_in_pairs(path: Path) -> None:
    sources = {src for _, src in _images(path.read_text(encoding="utf-8"))}
    swap = {"light": "dark", "dark": "light"}
    for src in sources:
        match = re.fullmatch(r"(.*)-(light|dark)(\.\w+)#gh-(light|dark)-mode-only", src)
        if match is None:
            assert "-mode-only" not in src, src
            continue
        stem, theme, ext, mode = match.groups()
        assert theme == mode, f"{src} is marked for the other theme"
        other = swap[theme]
        assert f"{stem}-{other}{ext}#gh-{other}-mode-only" in sources, f"{src} has no {other} pair"


@pytest.mark.parametrize("path", SHARED_MARKDOWN, ids=_ids)
def test_local_images_exist(path: Path) -> None:
    for _, src in _images(path.read_text(encoding="utf-8")):
        if src.startswith(RAW):
            target = ROOT / src.removeprefix(RAW)
        elif src.startswith(("http://", "https://")):
            continue
        else:
            target = path.parent / src
        assert target.with_name(target.name.split("#", 1)[0]).is_file(), src


# --------------------------------------------------------------------------- neutral content


@pytest.mark.parametrize("path", SHARED_MARKDOWN, ids=_ids)
def test_only_neutral_hosts(path: Path) -> None:
    assert _host_problems(path.read_text(encoding="utf-8")) == []


def test_host_check_rejects_real_looking_hosts() -> None:
    assert _host_problems("see https://jira.corp.invalid/x") == ["jira.corp.invalid"]
    assert _host_problems("`--base-url https://...` and https://HOST/jira") == []
    assert _host_problems("https://jira.example.test/jira and https://github.com/x") == []


@pytest.mark.parametrize("path", sorted((DOCS / "assets").rglob("*.svg")), ids=_ids)
def test_svg_assets_name_only_example_hosts(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    hosts = re.findall(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:test|com|net|org|io))\b", text)
    # Textual's SVG export names the SVG namespace, its own site, and its web font source.
    allowed = ("example.test", "www.w3.org", "www.textualize.io", "cdnjs.cloudflare.com")
    assert [h for h in hosts if not h.endswith(allowed)] == []


# --------------------------------------------------------------------------- README first screen


def test_readme_first_screen_has_banner_pitch_install_and_demo() -> None:
    text = README.read_text(encoding="utf-8")
    first = text.split("\n## ", 1)[0]
    assert len(first.splitlines()) <= 40, "keep the first screen short"
    assert "<picture>" in first and 'media="(prefers-color-scheme: dark)"' in first
    assert f"{RAW}docs/assets/banner-dark.svg" in first
    assert f"{RAW}docs/assets/banner-light.svg" in first
    assert re.search(r"^pipx install \S+", first, re.M), "a one-line install"
    assert f"{RAW}docs/assets/demos/one-call.gif" in first
    assert "fixture" in first and "not a live instance" in first
    install = re.search(r"^pipx install (\S+)", first, re.M)
    assert install and install.group(1) != "yojo", "yojo is not on PyPI until the first release"


def test_readme_demo_command_is_the_taped_command() -> None:
    tape = DOCS / "demos" / "one-call.tape"
    if not tape.is_file():
        pytest.skip("the demo tape is not recorded yet")
    command = (
        'yojo jira issue create PROJ --type Bug --summary "Fix example sign-in" '
        "--base-url https://jira.example.test/jira"
    )
    assert command in tape.read_text(encoding="utf-8")
    alt = dict((src, alt) for alt, src in _images(README.read_text(encoding="utf-8")))
    assert "Fix example sign-in" in alt[f"{RAW}docs/assets/demos/one-call.gif"]


# --------------------------------------------------------------------------- setup gallery


def test_setup_guide_shows_every_step_in_both_themes() -> None:
    text = (DOCS / "getting-started.md").read_text(encoding="utf-8")
    images = dict((src, alt) for alt, src in _images(text))
    for step in range(1, SETUP_STEPS + 1):
        for theme in ("light", "dark"):
            src = f"assets/setup/step-{step}-{theme}.svg#gh-{theme}-mode-only"
            assert src in images, src
            assert f"step {step} of {SETUP_STEPS}" in images[src]
            assert (DOCS / src.split("#", 1)[0]).is_file()
    assert len(list((DOCS / "assets" / "setup").glob("step-*.svg"))) == 2 * SETUP_STEPS


# What each setup screenshot must say it shows, beyond its step number.
SETUP_STEP_ALT = {
    1: ("Connect", "URLs", "server version"),
    2: ("Log in", "masked token", "keyring"),
    3: ("Choose projects", "search", "repository"),
    4: ("Map fields and rules", "required on create", "transition rules"),
    5: ("Review and save", "diff", "yojo.yaml", "before Save"),
    6: ("Connect your agent", "AGENTS.md"),
    7: ("Try it", "read-only", "next steps"),
}


def test_setup_alt_text_describes_each_step() -> None:
    text = (DOCS / "getting-started.md").read_text(encoding="utf-8")
    images = dict((src.split("#", 1)[0], alt) for alt, src in _images(text))
    alts = []
    for step, words in SETUP_STEP_ALT.items():
        light = images[f"assets/setup/step-{step}-light.svg"]
        assert images[f"assets/setup/step-{step}-dark.svg"] == light
        assert light.startswith(f"yojo setup step {step} of {SETUP_STEPS}, {words[0]}:"), light
        for word in words[1:]:
            assert word in light, (step, word)
        alts.append(light.split(":", 1)[1])
    assert len(set(alts)) == SETUP_STEPS
    readme = dict((src, alt) for alt, src in _images(README.read_text(encoding="utf-8")))
    step5 = readme[f"{RAW}docs/assets/setup/step-5-light.svg"]
    assert all(word in step5 for word in ("step 5 of 7", "diff", "nothing is written"))


# --------------------------------------------------------------------------- diagram meaning


def _mermaid(page: Path, marker: str) -> str:
    blocks: list[str] = re.findall(r"```mermaid\n(.*?)```", page.read_text(encoding="utf-8"), re.S)
    (block,) = [b for b in blocks if marker in b]
    return block


def _flow(block: str) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """Node labels and solid edges (source, label, target) of a Mermaid flowchart."""
    nodes = {
        node: label.replace("<br/>", " ")
        for node, label in re.findall(r'(\w+)\s*[\[\{\(]+"([^"]*)"', block)
    }
    edges = []
    for line in block.splitlines():
        parts = re.split(r'\s*-->(?:\|"?([^"|]*)"?\|)?\s*', line.strip())
        ids = [m.group(0) for m in (re.match(r"\w+", part) for part in parts[0::2]) if m]
        if len(ids) != len(parts[0::2]):
            continue
        labels = [(label or "").replace("<br/>", " ") for label in parts[1::2]]
        edges += [(a, label, b) for a, label, b in zip(ids, labels, ids[1:], strict=False)]
    return nodes, edges


def _targets(edges: list[tuple[str, str, str]], source: str) -> dict[str, str]:
    return {label: target for a, label, target in edges if a == source}


@pytest.mark.parametrize("page", [README, DOCS / "index.md"], ids=_ids)
def test_write_flow_diagram_keeps_its_safety_meaning(page: Path) -> None:
    nodes, edges = _flow(_mermaid(page, "Refused"))
    assert "nothing was sent" in nodes["Refused"]
    refused = _targets(edges, "Refused")
    (given,) = [t for label, t in refused.items() if "details.retry given" in label]
    assert given == "Call", "only a given retry loops back to the call"
    (absent,) = [t for label, t in refused.items() if "null or absent" in label]
    assert absent != "Call" and "Follow each fix" in nodes[absent]
    assert "sent once" in nodes["Write"]
    assert _targets(edges, "Confirm") == {"yes": "Done", "cannot tell": "Unknown"}
    assert "no retry is offered" in nodes["Unknown"]
    assert set(_targets(edges, "Unknown").values()) == {"Inspect"}, "inspect before any rerun"
    assert _targets(edges, "Inspect") == {"not applied": "Call", "applied, or unsure": "Stop"}
    assert "ask a person" in nodes["Stop"]


@pytest.mark.parametrize("page", [README, DOCS / "index.md", DOCS / "agents.md"], ids=_ids)
def test_roles_diagram_keeps_approval_and_secrets_with_people(page: Path) -> None:
    block = _mermaid(page, "subgraph Person")
    groups = dict(re.findall(r"subgraph (\w+)\[[^\]]*\]\n(.*?)\n\s*end", block, re.S))
    assert set(groups) == {"Person", "Agent", "Yojo"}
    assert "Approves and merges" in groups["Person"] and "keyring" in groups["Person"]
    for other in ("Agent", "Yojo"):
        assert not re.search(r"approve|merge|token", groups[other], re.I), other
    assert "never retries" in groups["Yojo"] and "before the write" in groups["Yojo"]
    assert "inspects before rerunning" in groups["Agent"]
    _, edges = _flow(block)
    assert ("Agent", "exit 3, or unsure", "Person") in edges


def test_agent_loop_diagram_retries_only_when_offered() -> None:
    nodes, edges = _flow(_mermaid(DOCS / "agents.md", "Verify"))
    verify = _targets(edges, "Verify")
    ((label, refused),) = [(label, t) for label, t in verify.items() if "exit 4" in label]
    assert "nothing was sent" in label
    assert "only if it is present" in nodes[refused]
    (unknown,) = [t for label, t in verify.items() if "outcome_unknown" in label]
    assert "before anything else" in nodes[unknown]
    assert _targets(edges, unknown) == {}, "no path from an unknown outcome straight to a write"


def test_transition_diagram_stops_before_any_unchecked_write() -> None:
    nodes, edges = _flow(_mermaid(DOCS / "guides" / "jira-workflows.md", "Ready"))
    assert _targets(edges, "Ready")["no, or unverified"] == "Stop"
    assert "nothing is written" in nodes["Stop"]
    recheck = _targets(edges, "Recheck")
    assert "no POST" in nodes[recheck["no, or refresh failed"]]
    assert "partial_transition" in nodes[recheck["no, or refresh failed"]]
    assert "POST" in nodes[recheck["yes"]]


def test_login_diagram_stores_nothing_on_failure() -> None:
    block = _mermaid(DOCS / "getting-started.md", "Keyring")
    assert block.count("nothing stored") == 2
    assert "keyring_unavailable" in block and "auth_failed" in block
    stored = block.index("Store under")
    assert block.index("2xx with an authenticated user") < stored < block.index("else 401")


# --------------------------------------------------------------------------- agent install lines


@pytest.mark.parametrize("page", [README, DOCS / "agents.md"], ids=_ids)
def test_every_agent_target_has_a_copyable_install_line(page: Path) -> None:
    text = page.read_text(encoding="utf-8")
    blocks = "\n".join(re.findall(r"```bash\n(.*?)```", text, re.S))
    for name, target in TARGETS.items():
        line = re.search(rf"^yojo --json agent install --target {name}\b.*$", blocks, re.M)
        assert line, f"no copyable install line for {name}"
        assert target.path in line.group(0), f"{name} line should name {target.path}"


# --------------------------------------------------------------------------- Pages workflow


@pytest.fixture(scope="module")
def pages() -> dict[Any, Any]:
    loaded = yaml.safe_load(PAGES_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_pages_deploys_only_after_ci_on_current_main(pages: dict[Any, Any]) -> None:
    triggers = pages[True]  # YAML 1.1 reads the `on` key as True
    assert set(triggers) == {"workflow_run", "workflow_dispatch"}
    assert triggers["workflow_run"] == {
        "workflows": ["CI"],
        "types": ["completed"],
        "branches": ["main"],
    }
    build = pages["jobs"]["build"]
    assert "workflow_run.conclusion == 'success'" in build["if"]
    assert "workflow_run.head_branch == 'main'" in build["if"]
    assert "workflow_run.head_sha" in build["env"]["DEPLOY_SHA"]
    verify = build["steps"][0]["run"]
    assert '"$DEPLOY_SHA" != "$main_sha"' in verify
    assert 'head_sha="$DEPLOY_SHA"' in verify
    assert '.conclusion == "success" and .event == "push"' in verify
    assert build["steps"][1]["with"]["ref"] == "${{ env.DEPLOY_SHA }}"
    deploy = pages["jobs"]["deploy"]
    assert deploy["if"] == "github.ref == 'refs/heads/main'"
    assert deploy["env"]["DEPLOY_SHA"] == build["env"]["DEPLOY_SHA"]
    # The deploy job repeats the gate immediately before deploy-pages, after the build finished.
    assert deploy["steps"][0]["run"] == verify
    assert deploy["steps"][1]["uses"].startswith("actions/deploy-pages@")
    assert len(deploy["steps"]) == 2
    assert pages["concurrency"] == {"group": "pages", "cancel-in-progress": False}


MAIN_SHA = "a" * 40
OLD_SHA = "b" * 40


def _ci_runs(*runs: tuple[str, str, str]) -> str:
    return json.dumps(
        {"workflow_runs": [{"head_sha": h, "conclusion": c, "event": e} for h, c, e in runs]}
    )


@pytest.mark.skipif(
    os.name == "nt" or not (shutil.which("bash") and shutil.which("jq")),
    reason="runs the workflow's bash gate with jq",
)
@pytest.mark.parametrize("job", ["build", "deploy"])
@pytest.mark.parametrize(
    ("event", "deploy_sha", "runs", "refusal"),
    [
        ("workflow_run", MAIN_SHA, _ci_runs((MAIN_SHA, "success", "push")), None),
        ("workflow_dispatch", MAIN_SHA, _ci_runs((MAIN_SHA, "success", "push")), None),
        ("workflow_run", OLD_SHA, _ci_runs((OLD_SHA, "success", "push")), "no longer the head"),
        (
            "workflow_dispatch",
            OLD_SHA,
            _ci_runs((OLD_SHA, "success", "push")),
            "no longer the head",
        ),
        ("workflow_dispatch", MAIN_SHA, _ci_runs(), "CI has not passed"),
        (
            "workflow_dispatch",
            MAIN_SHA,
            _ci_runs((MAIN_SHA, "failure", "push")),
            "CI has not passed",
        ),
        ("workflow_run", MAIN_SHA, _ci_runs((MAIN_SHA, "success", "pull_request")), "CI has not"),
        ("workflow_run", MAIN_SHA, _ci_runs((OLD_SHA, "success", "push")), "CI has not passed"),
    ],
)
def test_pages_gate_refuses_stale_main_and_unpassed_ci(
    pages: dict[Any, Any],
    tmp_path: Path,
    job: str,
    event: str,
    deploy_sha: str,
    runs: str,
    refusal: str | None,
) -> None:
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        f'  *git/ref/heads/main*) echo "{MAIN_SHA}" ;;\n'
        '  *ci.yml/runs*) cat "$FAKE_RUNS" ;;\n'
        "  *) exit 9 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    (tmp_path / "runs.json").write_text(runs, encoding="utf-8")
    proc = subprocess.run(  # noqa: S603 - the committed workflow step, against a fake gh
        [shutil.which("bash") or "bash", "-c", pages["jobs"][job]["steps"][0]["run"]],
        env={
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "DEPLOY_SHA": deploy_sha,
            "GITHUB_EVENT_NAME": event,
            "GITHUB_REPOSITORY": "example/repo",
            "FAKE_RUNS": str(tmp_path / "runs.json"),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if refusal is None:
        assert proc.returncode == 0, proc.stdout + proc.stderr
    else:
        assert proc.returncode != 0
        assert refusal in proc.stdout


def test_pages_permissions_are_least_privilege(pages: dict[Any, Any]) -> None:
    assert pages["permissions"] == {"contents": "read"}
    jobs = pages["jobs"]
    assert jobs["build"]["permissions"] == {"contents": "read", "actions": "read"}
    assert jobs["deploy"]["permissions"] == {
        "actions": "read",
        "contents": "read",
        "pages": "write",
        "id-token": "write",
    }
    assert jobs["deploy"]["environment"]["name"] == "github-pages"


def test_pages_builds_strictly_with_pinned_actions(pages: dict[Any, Any]) -> None:
    steps = [s for job in pages["jobs"].values() for s in job["steps"]]
    assert any("mkdocs build --strict" in str(s.get("run", "")) for s in steps)
    for step in steps:
        if "uses" in step:
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
    assert "secrets." not in PAGES_WORKFLOW.read_text(encoding="utf-8")
