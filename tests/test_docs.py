from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from atlass_cli.config import parse_config_file
from atlass_cli.contracts import COMMANDS_BY_PATH, ERROR_CODES, EXIT_CODES_DOC
from scripts import generate_command_reference as gen
from scripts.mkdocs_hooks import DocsRenderError, render_os_tabs, rewrite_external_links

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
REPO_URL = "https://github.com/calvinware/atlass-cli"
SHARED_MARKDOWN = [ROOT / "README.md", ROOT / "AGENTS.md", *sorted(DOCS.rglob("*.md"))]


@pytest.fixture(scope="module")
def discovered() -> tuple[list[gen.Contract], dict[str, str]]:
    return gen.discover()


@pytest.fixture
def examples() -> dict[str, Any]:
    return gen._load_json(gen.EXAMPLES_PATH, "commands")


@pytest.fixture
def fixes() -> dict[str, Any]:
    return gen._load_json(gen.FIXES_PATH, "codes")


# --------------------------------------------------------------------------- generated reference


def test_discovery_matches_registry(discovered: tuple[list[gen.Contract], dict[str, str]]) -> None:
    contracts, exit_codes = discovered
    assert [c.path for c in contracts] == list(COMMANDS_BY_PATH)
    assert exit_codes == EXIT_CODES_DOC


def test_committed_reference_is_current(
    discovered: tuple[list[gen.Contract], dict[str, str]],
    examples: dict[str, Any],
    fixes: dict[str, Any],
) -> None:
    contracts, exit_codes = discovered
    outputs = {
        gen.COMMANDS_MD: gen.render_commands(contracts, exit_codes, examples),
        gen.ERRORS_MD: gen.render_errors(contracts, exit_codes, fixes),
    }
    assert gen.check(outputs) == [], "run scripts/generate_command_reference.py --write"


def test_examples_are_complete_and_schema_valid(
    discovered: tuple[list[gen.Contract], dict[str, str]], examples: dict[str, Any]
) -> None:
    contracts, _ = discovered
    assert set(examples) == {c.path for c in contracts}
    assert gen.validate_examples(contracts, examples) == []
    for path, example in examples.items():
        code = example["error"]["error"]["code"]
        assert code in ERROR_CODES, path


def test_error_catalog_is_bijective(
    discovered: tuple[list[gen.Contract], dict[str, str]], fixes: dict[str, Any]
) -> None:
    contracts, _ = discovered
    assert set(fixes) == set(ERROR_CODES)
    assert gen.validate_fixes(contracts, fixes) == []
    errors_md = gen.ERRORS_MD.read_text(encoding="utf-8")
    for code in ERROR_CODES:
        assert f"### `{code}`" in errors_md


def test_counts_come_from_discovery(
    discovered: tuple[list[gen.Contract], dict[str, str]],
    examples: dict[str, Any],
    fixes: dict[str, Any],
) -> None:
    contracts, exit_codes = discovered
    subset = contracts[:3]
    assert "**3 commands**" in gen.render_commands(subset, exit_codes, examples)
    assert f"**{len(ERROR_CODES)} error codes**" in gen.render_errors(subset, exit_codes, fixes)


def test_missing_example_is_reported(
    discovered: tuple[list[gen.Contract], dict[str, str]], examples: dict[str, Any]
) -> None:
    contracts, _ = discovered
    del examples["jira search"]
    problems = gen.validate_examples(contracts, examples)
    assert any("no example for" in p and "jira search" in p for p in problems)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda e: e["success"]["data"].pop("key"), "success"),
        (lambda e: e["error"]["error"].update(code="partial_transition"), "not in this command"),
        (lambda e: e["input"].pop("summary"), "input"),
        (lambda e: e.update(argv=["jira", "story", "create"]), "argv"),
        (lambda e: e["success"]["data"].update(url="https://jira.corp.invalid/x"), "URL host"),
        (lambda e: e.update(note="Authorization: Bearer abc"), "credential"),
    ],
)
def test_invalid_examples_are_reported(
    discovered: tuple[list[gen.Contract], dict[str, str]],
    examples: dict[str, Any],
    mutate: Any,
    expected: str,
) -> None:
    contracts, _ = discovered
    broken = copy.deepcopy(examples)
    mutate(broken["jira story create"])
    problems = gen.validate_examples(contracts, broken)
    assert any(expected in p for p in problems), problems


def test_missing_error_fix_is_reported(
    discovered: tuple[list[gen.Contract], dict[str, str]], fixes: dict[str, Any]
) -> None:
    contracts, _ = discovered
    del fixes["outcome_unknown"]
    fixes["made_up_code"] = {"meaning": "x", "fix": "y"}
    problems = gen.validate_fixes(contracts, fixes)
    assert any("no entry for" in p and "outcome_unknown" in p for p in problems)
    assert any("unknown codes" in p and "made_up_code" in p for p in problems)


def _fake_atl(paths: list[str]) -> Any:
    real = gen.run_atl

    def fake(*args: str) -> Any:
        if args == ("commands",):
            data = real("commands")
            data["commands"] = [{"path": p, "summary": "", "schema": ""} for p in paths]
            return data
        return real(*args)

    return fake


def test_duplicate_command_paths_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    paths = [*COMMANDS_BY_PATH, "jira search"]
    monkeypatch.setattr(gen, "run_atl", _fake_atl(paths))
    with pytest.raises(gen.GenerationError, match="duplicate"):
        gen.discover()


def test_registry_drift_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    paths = [*COMMANDS_BY_PATH, "jira issue delete"]
    monkeypatch.setattr(gen, "run_atl", _fake_atl(paths))
    with pytest.raises(gen.GenerationError, match="does not match COMMANDS_BY_PATH"):
        gen.discover()


def test_check_mode_fails_on_stale_file_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stale_commands = tmp_path / "commands.md"
    stale_errors = tmp_path / "errors.md"
    stale_commands.write_text(
        gen.COMMANDS_MD.read_text(encoding="utf-8").replace("`atl jira search`", "`atl jira find`"),
        encoding="utf-8",
    )
    stale_errors.write_text(gen.ERRORS_MD.read_text(encoding="utf-8"), encoding="utf-8")
    before = stale_commands.read_bytes()
    monkeypatch.setattr(gen, "COMMANDS_MD", stale_commands)
    monkeypatch.setattr(gen, "ERRORS_MD", stale_errors)
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    assert gen.main(["--check"]) == 1
    assert "stale" in capsys.readouterr().err
    assert stale_commands.read_bytes() == before


def test_check_ignores_crlf_checkouts(tmp_path: Path) -> None:
    path = tmp_path / "page.md"
    path.write_bytes(b"line one\r\nline two\r\n")
    assert gen.check({path: "line one\nline two\n"}) == []


# --------------------------------------------------------------------------- site hooks

OS_BLOCK = """intro

<!-- os-tabs:start -->

### Windows

Run `py`.

### macOS

```bash
### not a heading inside a fence
brew install python@3.11
```

### Linux

Use pipx.

<!-- os-tabs:end -->

after
"""


def test_os_tabs_render_as_material_tabs() -> None:
    out = render_os_tabs(OS_BLOCK)
    assert '=== "Windows"\n\n    Run `py`.' in out
    assert '=== "macOS"\n\n    ```bash\n    ### not a heading inside a fence' in out
    assert '=== "Linux"\n\n    Use pipx.' in out
    assert "<!-- os-tabs" not in out
    assert out.startswith("intro\n") and out.rstrip().endswith("after")


@pytest.mark.parametrize(
    ("markdown", "message"),
    [
        (OS_BLOCK.replace("<!-- os-tabs:end -->", ""), "without a matching"),
        (OS_BLOCK.replace("<!-- os-tabs:start -->", ""), "without a matching"),
        (OS_BLOCK.replace("### Linux", "### FreeBSD"), "must be exactly"),
        (OS_BLOCK.replace("### Windows", "Windows"), "text before the first"),
        (OS_BLOCK.replace("Use pipx.", ""), "is empty"),
        (OS_BLOCK.replace("Run `py`.", "<!-- os-tabs:start -->"), "nested"),
    ],
)
def test_malformed_os_tabs_fail(markdown: str, message: str) -> None:
    with pytest.raises(DocsRenderError, match=message):
        render_os_tabs(markdown)


def test_getting_started_has_github_readable_os_sections() -> None:
    source = (DOCS / "getting-started.md").read_text(encoding="utf-8")
    assert source.count("<!-- os-tabs:start -->") == 2
    assert "=== " not in source
    for title in ("Windows", "macOS", "Linux"):
        assert source.count(f"### {title}\n") == 2
    assert render_os_tabs(source).count('=== "Linux"') == 2


def test_links_outside_docs_point_at_github_source() -> None:
    markdown = (
        "See [`AGENTS.md`](../../AGENTS.md#contract), [cli](../../src/atlass_cli/cli.py), "
        "[page](../configuration.md#drift-checks), [anchor](#local), "
        "[web](https://example.test/x), and `[code](../AGENTS.md)`.\n"
        "```\n[fenced](../AGENTS.md)\n```"
    )
    out = rewrite_external_links(markdown, "guides/page.md", REPO_URL)
    assert f"[`AGENTS.md`]({REPO_URL}/blob/main/AGENTS.md#contract)" in out
    assert f"({REPO_URL}/blob/main/src/atlass_cli/cli.py)" in out
    assert "(../configuration.md#drift-checks)" in out
    assert "(#local)" in out and "(https://example.test/x)" in out
    assert "`[code](../AGENTS.md)`" in out
    assert "[fenced](../AGENTS.md)" in out
    top = rewrite_external_links("[a](../AGENTS.md#contract)", "agents.md", REPO_URL)
    assert top == f"[a]({REPO_URL}/blob/main/AGENTS.md#contract)"


def test_links_leaving_the_repository_fail() -> None:
    with pytest.raises(DocsRenderError, match="leaves the repository"):
        rewrite_external_links("[x](../../outside.md)", "index.md", REPO_URL)


# --------------------------------------------------------------------------- shared Markdown rules


@pytest.mark.parametrize("path", SHARED_MARKDOWN, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_shared_markdown_has_no_site_only_syntax(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"^\s*!!!", text, re.M), "use GitHub alerts, not !!! admonitions"
    assert not re.search(r'^\s*===\s+"', text, re.M), "use os-tabs markers, not raw tabs"


@pytest.mark.parametrize(
    ("page", "count"),
    [
        ("guides/jira-workflows.md", 3),
        ("getting-started.md", 1),
        ("contributing.md", 1),
    ],
)
def test_required_diagrams_are_mermaid_fences(page: str, count: int) -> None:
    text = (DOCS / page).read_text(encoding="utf-8")
    assert text.count("```mermaid\n") >= count


def test_cookbook_recipes_are_valid_config(tmp_path: Path) -> None:
    text = (DOCS / "admin-cookbook.md").read_text(encoding="utf-8")
    recipes = [b for b in re.findall(r"```yaml\n(.*?)```", text, re.S) if b.startswith("version:")]
    assert len(recipes) >= 4
    for index, recipe in enumerate(recipes):
        path = tmp_path / f"recipe{index}.yaml"
        path.write_text(recipe, encoding="utf-8")
        parse_config_file(path)


def test_example_json_files_are_valid_json() -> None:
    for name in ("commands.json", "error-fixes.json"):
        json.loads((DOCS / "examples" / name).read_text(encoding="utf-8"))
