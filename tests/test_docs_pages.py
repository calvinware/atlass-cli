"""Checks on the hand-written pages: images, neutral hosts, the README's first screen, the setup
gallery, agent install lines, and the Pages workflow. No network is used."""

from __future__ import annotations

import re
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
RAW = "https://raw.githubusercontent.com/calvinware/yojo/main/"

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
    assert 'test "$DEPLOY_SHA" = "$main_sha"' in verify
    assert 'head_sha="$DEPLOY_SHA"' in verify
    assert '.conclusion == "success" and .event == "push"' in verify
    assert build["steps"][1]["with"]["ref"] == "${{ env.DEPLOY_SHA }}"
    assert pages["jobs"]["deploy"]["if"] == "github.ref == 'refs/heads/main'"
    assert pages["concurrency"] == {"group": "pages", "cancel-in-progress": False}


def test_pages_permissions_are_least_privilege(pages: dict[Any, Any]) -> None:
    assert pages["permissions"] == {"contents": "read"}
    jobs = pages["jobs"]
    assert jobs["build"]["permissions"] == {"contents": "read", "actions": "read"}
    assert jobs["deploy"]["permissions"] == {"pages": "write", "id-token": "write"}
    assert jobs["deploy"]["environment"]["name"] == "github-pages"


def test_pages_builds_strictly_with_pinned_actions(pages: dict[Any, Any]) -> None:
    steps = [s for job in pages["jobs"].values() for s in job["steps"]]
    assert any("mkdocs build --strict" in str(s.get("run", "")) for s in steps)
    for step in steps:
        if "uses" in step:
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
    assert "secrets." not in PAGES_WORKFLOW.read_text(encoding="utf-8")
