from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from scripts import capture_command_examples as capture
from scripts import generate_command_reference as gen
from scripts.mkdocs_hooks import DocsRenderError, render_os_tabs, rewrite_external_links
from tests.conftest import JIRA, Harness
from yojo.cli import NO_SERVICE_OPTIONS, build_parser
from yojo.config import parse_config_file
from yojo.contracts import COMMANDS_BY_PATH, ERROR_CODES, EXIT_CODES_DOC

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
REPO_URL = "https://github.com/calvinware/yojo"
SHARED_MARKDOWN = [ROOT / "README.md", ROOT / "AGENTS.md", *sorted(DOCS.rglob("*.md"))]


@pytest.fixture(scope="module")
def discovered() -> tuple[list[gen.Contract], dict[str, str]]:
    return gen.discover()


@pytest.fixture
def examples() -> dict[str, Any]:
    return gen.load_examples()


@pytest.fixture
def fixes() -> dict[str, Any]:
    return gen._load_json(gen.FIXES_PATH, "codes")


# --------------------------------------------------------------------------- generated reference


def test_discovery_matches_registry(discovered: tuple[list[gen.Contract], dict[str, str]]) -> None:
    contracts, exit_codes = discovered
    assert [c.path for c in contracts] == list(COMMANDS_BY_PATH)
    assert exit_codes == EXIT_CODES_DOC


@pytest.mark.parametrize("path", list(COMMANDS_BY_PATH))
def test_synopsis_offers_service_options_only_where_accepted(path: str, harness: Harness) -> None:
    parser = gen.leaf_parsers(build_parser())[path]
    line = gen.synopsis(path, parser)
    accepted = path not in NO_SERVICE_OPTIONS
    assert ("[--config PATH] [--base-url URL]" in line) == accepted, line
    assert line.endswith("[--json]")
    if not accepted:
        for option in ("--config", "--base-url"):
            result = harness.run(*path.split(), option, "value", "--jira-url", JIRA)
            assert result.error["code"] == "usage_error", (path, option)
            assert harness.server.requests == []


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
        (lambda e: e.update(input={"summary": 5}), "input"),
        (lambda e: e.update(argv=["jira", "widget", "create"]), "argv"),
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
    mutate(broken["jira issue create"])
    problems = gen.validate_examples(contracts, broken)
    assert any(expected in p for p in problems), problems


def _set(path: list[Any], value: Any) -> Any:
    def mutate(example: dict[str, Any]) -> None:
        target = example
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value

    return mutate


# Each mutation keeps every schema valid, so only the argv and semantic checks can catch it.
@pytest.mark.parametrize(
    ("command", "mutate", "expected"),
    [
        ("jira transition", _set(["argv", 2], "EXAMPLE-43"), "argv gives key='EXAMPLE-43'"),
        ("jira transition", _set(["argv", 4], "In Progress"), "argv gives to='In Progress'"),
        ("jira issue link", _set(["argv", 6], "relates"), "argv gives type='relates'"),
        ("jira search", _set(["argv", 5], "25"), "argv gives limit=25"),
        ("jira search", _set(["arguments", "start"], 0), None),
        ("jira search", _set(["arguments", "start"], 40), "argv gives start=0"),
        ("schema", _set(["argv", 1], "jira search"), "argv gives command='jira search'"),
        ("jira transition", _set(["success", "data", "key"], "EXAMPLE-43"), "success.data.key"),
        ("jira requirements", _set(["success", "data", "to"], "Done"), "success.data.to"),
        ("jira issue get", _set(["success", "data", "key"], "EXAMPLE-43"), "success.data.key"),
        ("confluence page read", _set(["success", "data", "id"], "123457"), "success.data.id"),
        (
            "confluence page update",
            _set(["input", "expected_version"], 2),
            "success.data.previous_version",
        ),
        ("confluence page update", _set(["success", "data", "version"], 5), "success.data.version"),
        (
            "confluence page update",
            _set(["error", "error", "details", "current_version"], 3),
            "different current_version",
        ),
        ("confluence page create", _set(["success", "data", "title"], "Other"), "data.title"),
        (
            "jira issue create",
            _set(["success", "data", "url"], "https://jira.example.test/x"),
            "url",
        ),
        (
            "jira attachment add",
            _set(["success", "data", "attachments", 0, "filename"], "other.xml"),
            "filenames",
        ),
        ("jira requirements", _set(["success", "data", "ready"], False), "ready is False"),
        (
            "jira issue create",
            _set(["error", "error", "details", "retry"], "yojo jira issue create EXAMPLE"),
            "retry is not the shell-quoted",
        ),
        (
            "jira transition",
            _set(["error", "error", "details", "checks", 0, "status"], "pass"),
            "at least one failing check",
        ),
        (
            "jira transition",
            _set(["success", "data", "operations", 0, "status"], "failed"),
            "every operation succeeded",
        ),
        ("config check", _set(["success", "data", "summary"], {"pass": 8}), "summary"),
    ],
)
def test_examples_must_match_their_call(
    discovered: tuple[list[gen.Contract], dict[str, str]],
    examples: dict[str, Any],
    command: str,
    mutate: Any,
    expected: str | None,
) -> None:
    contracts, _ = discovered
    broken = copy.deepcopy(examples)
    mutate(broken[command])
    problems = gen.validate_examples(contracts, broken)
    if expected is None:
        assert problems == []
        return
    assert any(expected in p and command in p for p in problems), problems


def test_comment_body_file_schema_and_row_say_plain_text(
    discovered: tuple[list[gen.Contract], dict[str, str]],
) -> None:
    contracts, _ = discovered
    (comment,) = [c for c in contracts if c.path == "jira comment add"]
    prop = comment.schema["arguments"]["properties"]["body_file"]
    assert "plain UTF-8 text file" in prop["description"]
    assert "JSON file" not in prop["description"]
    assert prop["minLength"] == 1
    reference = gen.COMMANDS_MD.read_text(encoding="utf-8")
    rows = [line for line in reference.splitlines() if line.startswith("| `body_file` |")]
    assert len(rows) == 2  # jira comment add and bitbucket pr comment share the schema
    assert all(prop["description"] in row for row in rows)


def _embedded_schema_drift(contract: gen.Contract, example: dict[str, Any]) -> list[str]:
    data = example["success"]["data"]
    return [k for k in contract.schema if data.get(k) != contract.schema[k]]


def test_embedded_comment_schema_matches_discovered(
    discovered: tuple[list[gen.Contract], dict[str, str]], examples: dict[str, Any]
) -> None:
    contracts, _ = discovered
    (comment,) = [c for c in contracts if c.path == "jira comment add"]
    example = examples["schema"]
    assert example["arguments"] == {"command": "jira comment add"}
    assert _embedded_schema_drift(comment, example) == []

    reverted = copy.deepcopy(example)
    body_file = reverted["success"]["data"]["arguments"]["properties"]["body_file"]
    body_file["description"] = "Path to a JSON file, or '-' for stdin."
    assert _embedded_schema_drift(comment, reverted) == ["arguments"]


# --------------------------------------------------------------------------- captured output

REMOTE_WRITES = [
    "jira issue create",
    "jira issue update",
    "jira issue link",
    "jira transition",
    "jira attachment add",
    "jira comment add",
    "confluence page create",
    "confluence page update",
    "bitbucket pr create",
    "bitbucket pr comment",
]


def test_every_example_has_both_faces_captured(examples: dict[str, Any]) -> None:
    for path, example in examples.items():
        captured = example["captured"]
        for which in ("success", "error"):
            case = captured[which]
            if case is None:
                assert which == "success" and path in gen.INTERACTIVE, path
                continue
            assert len(case["json"].splitlines()) == 1, (path, which)
            assert (case["terminal"] is None) == (path in gen.INTERACTIVE), (path, which)


def test_successful_writes_send_one_write_and_failures_none(examples: dict[str, Any]) -> None:
    for path in REMOTE_WRITES:
        success = examples[path]["captured"]["success"]
        assert success["writes"] == 1, path
        assert json.loads(success["json"])["ok"] is True, path
    for path, example in examples.items():
        assert example["captured"]["error"]["writes"] == 0, path


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda e: e["captured"].pop("success"), "captured success is missing"),
        (lambda e: e.update(error_argv=["jira", "issue", "get", "EXAMPLE-1"]), "different call"),
        (lambda e: e["captured"]["success"].update(exit=4), "exit 4"),
        (lambda e: e["captured"]["error"].update(writes=1), "send no write"),
        (lambda e: e["captured"]["success"].update(terminal=None), "terminal capture"),
        (
            lambda e: e["captured"]["success"].update(json=json.dumps(e["success"], indent=2)),
            "exactly one line",
        ),
        (
            lambda e: e["captured"]["success"].update(json=json.dumps(e["success"])),
            "not compact",
        ),
    ],
)
def test_capture_drift_is_reported(
    discovered: tuple[list[gen.Contract], dict[str, str]],
    examples: dict[str, Any],
    mutate: Any,
    expected: str,
) -> None:
    contracts, _ = discovered
    broken = copy.deepcopy(examples)
    mutate(broken["jira issue create"])
    problems = gen.validate_examples(contracts, broken)
    assert any(expected in p and "jira issue create" in p for p in problems), problems


def test_reference_shows_exact_output_in_both_faces(examples: dict[str, Any]) -> None:
    reference = gen.COMMANDS_MD.read_text(encoding="utf-8")
    for path, example in examples.items():
        for case in filter(None, example["captured"].values()):
            assert case["json"].rstrip("\n") in reference, path
            if case["terminal"] is not None:
                assert case["terminal"].rstrip("\n") in reference, path
    notes = [e.get("note", "") for e in examples.values()]
    assert not [n for n in notes if "shortened" in n or "abbreviated" in n]
    assert "pretty-printed" not in reference


def test_terminal_capture_must_render_the_same_envelope(examples: dict[str, Any]) -> None:
    case = copy.deepcopy(examples["jira issue create"]["captured"]["success"])
    assert capture.problems_in("jira issue create", "success", case) == []
    case["terminal"] = case["terminal"].replace("Created", "Updated")
    assert any(
        "not the rendering" in p for p in capture.problems_in("jira issue create", "success", case)
    )
    leaked = copy.deepcopy(examples["login"]["captured"]["success"])
    leaked["json"] = leaked["json"].replace("example-user", "jira-secret-token-123")
    assert any("token" in p for p in capture.problems_in("login", "success", leaked))


@pytest.mark.skipif(os.name == "nt", reason="captures are written on POSIX paths")
def test_committed_captures_are_current() -> None:
    expected = capture.CAPTURED_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert capture.serialize(capture.build()) == expected, (
        "run scripts/capture_command_examples.py --write"
    )


def test_capture_check_fails_on_stale_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = tmp_path / "captured-output.json"
    stale.write_text(
        capture.CAPTURED_PATH.read_text(encoding="utf-8").replace("Created EXAMPLE-42", "Made it")
    )
    before = stale.read_bytes()
    monkeypatch.setattr(capture, "CAPTURED_PATH", stale)
    monkeypatch.setattr(capture, "ROOT", tmp_path)
    assert capture.main_(["--check"]) == 1
    assert "capture_command_examples.py --write" in capsys.readouterr().err
    assert stale.read_bytes() == before


def test_missing_error_fix_is_reported(
    discovered: tuple[list[gen.Contract], dict[str, str]], fixes: dict[str, Any]
) -> None:
    contracts, _ = discovered
    del fixes["outcome_unknown"]
    fixes["made_up_code"] = {"meaning": "x", "fix": "y"}
    problems = gen.validate_fixes(contracts, fixes)
    assert any("no entry for" in p and "outcome_unknown" in p for p in problems)
    assert any("unknown codes" in p and "made_up_code" in p for p in problems)


def _fake_yojo(paths: list[str]) -> Any:
    real = gen.run_yojo

    def fake(*args: str) -> Any:
        if args == ("commands",):
            data = real("commands")
            data["commands"] = [{"path": p, "summary": "", "schema": ""} for p in paths]
            return data
        return real(*args)

    return fake


def test_duplicate_command_paths_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    paths = [*COMMANDS_BY_PATH, "jira search"]
    monkeypatch.setattr(gen, "run_yojo", _fake_yojo(paths))
    with pytest.raises(gen.GenerationError, match="duplicate"):
        gen.discover()


def test_registry_drift_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    paths = [*COMMANDS_BY_PATH, "jira issue delete"]
    monkeypatch.setattr(gen, "run_yojo", _fake_yojo(paths))
    with pytest.raises(gen.GenerationError, match="does not match COMMANDS_BY_PATH"):
        gen.discover()


def test_check_mode_fails_on_stale_file_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stale_commands = tmp_path / "commands.md"
    stale_errors = tmp_path / "errors.md"
    stale_commands.write_text(
        gen.COMMANDS_MD.read_text(encoding="utf-8").replace(
            "`yojo jira search`", "`yojo jira find`"
        ),
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
        "See [`AGENTS.md`](../../AGENTS.md#contract), [cli](../../src/yojo/cli.py), "
        "[page](../configuration.md#drift-checks), [anchor](#local), "
        "[web](https://example.test/x), and `[code](../AGENTS.md)`.\n"
        "```\n[fenced](../AGENTS.md)\n```"
    )
    out = rewrite_external_links(markdown, "guides/page.md", REPO_URL)
    assert f"[`AGENTS.md`]({REPO_URL}/blob/main/AGENTS.md#contract)" in out
    assert f"({REPO_URL}/blob/main/src/yojo/cli.py)" in out
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


README_BLOB_URL = f"{REPO_URL}/blob/main/"
README_RAW_URL = "https://raw.githubusercontent.com/calvinware/yojo/main/"
# Inline links (with optional <...> and title), reference definitions, and HTML attributes.
README_TARGET_RE = re.compile(
    r"\]\(\s*<?([^)\s<>]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)"
    r"|^ {0,3}\[[^\]]+\]:[ \t]*<?([^\s<>]+)>?"
    r"|\b(?:src|srcset|href)=\"([^\"]+)\"",
    re.M,
)


def _readme_link_targets(text: str) -> list[str]:
    lines = "\n".join(_outside_fences(text))
    return [next(group for group in m if group) for m in README_TARGET_RE.findall(lines)]


def _readme_link_problems(text: str) -> list[str]:
    problems = []
    for target in _readme_link_targets(text):
        if target.startswith("#"):
            continue
        if not target.startswith("https://"):
            problems.append(f"{target!r} is relative; PyPI cannot resolve it")
            continue
        prefix = next((p for p in (README_BLOB_URL, README_RAW_URL) if target.startswith(p)), None)
        if prefix is None:
            continue
        path = (ROOT / target.removeprefix(prefix).split("#", 1)[0]).resolve()
        if not (path.is_relative_to(ROOT) and path.is_file()):
            problems.append(f"{target!r} has no file in the repo")
    return problems


def test_readme_link_targets_are_absolute_repository_files() -> None:
    """PyPI renders README.md without the repository, so file links must be absolute.

    This checks link shapes and local file mappings only; it fetches no URL.
    """
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert len(_readme_link_targets(text)) > 10
    assert _readme_link_problems(text) == []
    for variant in ("dark", "light"):
        assert f'="{README_RAW_URL}docs/assets/banner-{variant}.svg"' in text


@pytest.mark.parametrize(
    "markdown",
    [
        "[guide](docs/getting-started.md)",
        '[guide](docs/getting-started.md "Guide")',
        "[guide](docs/getting-started.md 'Guide')",
        "[guide](<docs/getting-started.md>)",
        "[guide][ref]\n\n[ref]: docs/getting-started.md",
        '[ref]: docs/getting-started.md "Guide"',
        "   [ref]: <docs/getting-started.md>",
        '<img src="docs/assets/banner-dark.svg">',
        f'[gone]({README_BLOB_URL}docs/missing.md "Missing")',
        f"[up]: {README_BLOB_URL}../outside.md",
    ],
)
def test_readme_link_check_rejects_relative_or_missing_targets(markdown: str) -> None:
    assert _readme_link_problems(markdown)


def test_readme_link_check_accepts_titled_absolute_targets() -> None:
    markdown = (
        f'[guide]({README_BLOB_URL}docs/getting-started.md "Guide")\n'
        f"[ref]: {README_BLOB_URL}LICENSE 'License'\n"
        "```\n[fenced](docs/getting-started.md)\n```"
    )
    assert len(_readme_link_targets(markdown)) == 2
    assert _readme_link_problems(markdown) == []


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


# --------------------------------------------------------------------------- agent wrapper


def _ok(data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return 0, {"ok": True, "data": data}


def _err(
    exit_code: int, code: str, fix: str, details: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any]]:
    return exit_code, {
        "ok": False,
        "error": {"code": code, "message": "m", "fix": fix, "details": details or {}},
    }


REFUSED = {
    "nothing_sent": True,
    "checks": [
        {
            "id": "attachment:{key}_Evidence_*",
            "status": "fail",
            "message": "No matching attachment.",
            "fix": "Attach a supporting file.",
        }
    ],
    "passed": 2,
    "retry": "yojo jira transition EXAMPLE-42 --to Done",
    "retry_argv": ["yojo", "jira", "transition", "EXAMPLE-42", "--to", "Done"],
}


@pytest.fixture
def agent_wrapper(tmp_path: Path) -> dict[str, Any]:
    """Load the copyable Python wrapper from docs/agents.md exactly as published."""
    import runpy

    text = (DOCS / "agents.md").read_text(encoding="utf-8")
    (block,) = re.findall(r"```python\n(.*?)```", text, re.S)
    module = tmp_path / "agent_wrapper.py"
    module.write_text(block, encoding="utf-8")
    return runpy.run_path(str(module))


def _fake_cli(
    monkeypatch: pytest.MonkeyPatch, reply: tuple[int, dict[str, Any]]
) -> list[list[str]]:
    import subprocess
    from types import SimpleNamespace

    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        assert argv[0] == "yojo" and argv[-1] == "--json" and kwargs.get("shell") is not True
        calls.append(argv)
        exit_code, envelope = reply
        return SimpleNamespace(returncode=exit_code, stdout=json.dumps(envelope) + "\n")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_agent_wrapper_transitions_in_one_call(
    agent_wrapper: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    done = {"key": "EXAMPLE-42", "status": "Done", "operations": []}
    calls = _fake_cli(monkeypatch, _ok(done))
    assert agent_wrapper["transition"]("EXAMPLE-42", "Done", "Resolution=Won't Do") == done
    assert calls == [
        [
            "yojo",
            "jira",
            "transition",
            "EXAMPLE-42",
            "--to",
            "Done",
            "--set",
            "Resolution=Won't Do",
            "--json",
        ]
    ]


@pytest.mark.parametrize(
    ("reply", "messages"),
    [
        (
            _err(3, "auth_missing", "Ask a human to run yojo login."),
            ["Ask a human to run yojo login."],
        ),
        (
            _err(4, "requirements_unmet", "Apply each check's fix.", REFUSED),
            [
                "Apply each check's fix.",
                "attachment:{key}_Evidence_*: Attach a supporting file.",
                "yojo jira transition EXAMPLE-42 --to Done",
            ],
        ),
        (
            _err(6, "outcome_unknown", "Inspect the issue."),
            ["Inspect the issue.", "Writes and retries"],
        ),
        (
            _err(7, "partial_transition", "Do not roll back."),
            ["Do not roll back.", "Writes and retries"],
        ),
    ],
    ids=["auth-error", "refused", "exit-6", "exit-7"],
)
def test_agent_wrapper_stops_with_the_fix(
    agent_wrapper: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    reply: tuple[int, dict[str, Any]],
    messages: list[str],
) -> None:
    calls = _fake_cli(monkeypatch, reply)
    with pytest.raises(agent_wrapper["Stop"]) as raised:
        agent_wrapper["transition"]("EXAMPLE-42", "Done")
    for message in messages:
        assert message in str(raised.value)
    assert len(calls) == 1
    if reply[1]["error"]["code"] in ("outcome_unknown", "partial_transition"):
        assert "retry" not in str(raised.value).lower().replace("writes and retries", "")


def test_agent_wrapper_rejects_mismatched_exit_and_envelope(
    agent_wrapper: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_cli(monkeypatch, (0, {"ok": False, "error": {}}))
    with pytest.raises(agent_wrapper["Stop"], match="does not match"):
        agent_wrapper["transition"]("EXAMPLE-42", "Done")


# --------------------------------------------------------------------------- built site

ALERT_RE = re.compile(r"^> \[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*$")


def _outside_fences(text: str) -> list[str]:
    lines, in_fence = [], False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        elif not in_fence:
            lines.append(line)
    return lines


def _built_page(site: Path, page: Path) -> str:
    rel = page.relative_to(DOCS).with_suffix("")
    html = site / ("index.html" if rel.name == "index" else rel / "index.html")
    return html.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def built_site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the site once with the real config, hooks, and plugins into a temporary folder."""
    import importlib.metadata
    import importlib.util
    import os
    import subprocess
    import sys

    plugins = {ep.name for ep in importlib.metadata.entry_points(group="mkdocs.plugins")}
    if importlib.util.find_spec("material") is None or "gh-admonitions" not in plugins:
        if os.environ.get("YOJO_REQUIRE_SITE_BUILD") == "1":
            pytest.fail("YOJO_REQUIRE_SITE_BUILD=1 but the docs extra is not installed")
        pytest.skip("install the docs extra to build the site")
    site = tmp_path_factory.mktemp("site")
    proc = subprocess.run(  # noqa: S603 - fixed argv: this interpreter running MkDocs
        [
            sys.executable,
            "-m",
            "mkdocs",
            "build",
            "--strict",
            "-f",
            str(ROOT / "mkdocs.yml"),
            "-d",
            str(site),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "NO_MKDOCS_2_WARNING": "1"},
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return site


SITE_PAGES = sorted(DOCS.rglob("*.md"))


@pytest.mark.parametrize("page", SITE_PAGES, ids=lambda p: p.relative_to(DOCS).as_posix())
def test_built_site_styles_every_alert(built_site: Path, page: Path) -> None:
    lines = _outside_fences(page.read_text(encoding="utf-8"))
    alerts = [line for line in lines if ALERT_RE.match(line)]
    html = _built_page(built_site, page)
    assert html.count('class="admonition ') == len(alerts)
    assert not re.search(r"<p>\s*\[!", html), "an unconverted GitHub alert reached the site"


@pytest.mark.parametrize("page", SITE_PAGES, ids=lambda p: p.relative_to(DOCS).as_posix())
def test_built_site_keeps_every_diagram(built_site: Path, page: Path) -> None:
    source = page.read_text(encoding="utf-8")
    html = _built_page(built_site, page)
    diagrams = re.findall(r'<pre class="mermaid"><code>(.*?)</code></pre>', html, re.S)
    assert len(diagrams) == source.count("```mermaid\n")
    for diagram in diagrams:
        assert re.match(r"\s*(flowchart|sequenceDiagram|stateDiagram)", diagram), diagram[:80]


def test_built_site_renders_os_tabs(built_site: Path) -> None:
    html = _built_page(built_site, DOCS / "getting-started.md")
    assert html.count('class="tabbed-set tabbed-alternate"') == 2
    labels = re.findall(r'<label for="__tabbed_\d+_\d+">([^<]+)</label>', html)
    assert labels == ["Windows", "macOS", "Linux"] * 2
    assert "os-tabs:" not in html


def test_built_site_shows_one_banner_per_palette(built_site: Path) -> None:
    html = _built_page(built_site, DOCS / "index.md")
    assert 'src="assets/banner-light.svg#gh-light-mode-only"' in html
    assert 'src="assets/banner-dark.svg#gh-dark-mode-only"' in html
    assert 'href="stylesheets/extra.css"' in html
    css = re.sub(r"\s+", " ", (built_site / "stylesheets" / "extra.css").read_text("utf-8"))
    assert '[data-md-color-scheme="default"] img[src$="#gh-dark-mode-only"]' in css
    assert '[data-md-color-scheme="slate"] img[src$="#gh-light-mode-only"]' in css
    assert "display: none" in css


def test_built_site_rewrites_links_outside_docs(built_site: Path) -> None:
    html = _built_page(built_site, DOCS / "agents.md")
    assert f'href="{REPO_URL}/blob/main/AGENTS.md"' in html
