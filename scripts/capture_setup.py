"""Capture each `yojo setup` step as an SVG, in the light and the dark theme.

The app is driven with Textual's pilot against the test fixture server and an in-memory keyring,
in a temporary folder: no network, no OS keyring, no real token. Every capture is checked for the
fixture tokens before it is written.

    python scripts/capture_setup.py --write   # regenerate docs/assets/setup/*.svg
    python scripts/capture_setup.py --check   # exit 1 if the committed captures drift
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import tempfile
from collections.abc import Callable
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from textual.widgets import Input, Select, SelectionList  # noqa: E402

from tests.conftest import (  # noqa: E402
    BITBUCKET_TOKEN,
    CONFLUENCE_TOKEN,
    JIRA_TOKEN,
    FakeKeyring,
    FakeServer,
)
from tests.setup_support import URLS, add_routes, runtime  # noqa: E402
from yojo.setup import ReviewStep, Services, SetupApp  # noqa: E402

OUT = ROOT / "docs" / "assets" / "setup"
THEMES = {"light": "textual-light", "dark": "textual-dark"}
SIZE = (100, 40)
TOKENS = {"jira": JIRA_TOKEN, "confluence": CONFLUENCE_TOKEN, "bitbucket": BITBUCKET_TOKEN}


async def _capture(theme: str, cwd: Path) -> list[str]:
    app = SetupApp(Services(runtime(cwd, add_routes(FakeServer()), FakeKeyring())), theme=theme)
    app.animation_level = "none"
    shots: list[str] = []
    async with app.run_test(size=SIZE) as pilot:
        # Keep the cursor in a fixed state while exporting SVGs.
        for widget in app.query(Input):
            widget.cursor_blink = False

        async def settle() -> None:
            # Let the input event create its worker before waiting for workers.
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause(0.05)

        async def wait_until(ready: Callable[[], bool]) -> None:
            for _ in range(40):
                await settle()
                if ready():
                    return
            raise RuntimeError(f"setup did not finish step {app.step} before capture")

        def has_login(service: str) -> bool:
            return service in app.draft.logins

        async def press(selector: str) -> None:
            app.query_one(selector).press()  # type: ignore[attr-defined]
            await settle()

        async def advance() -> None:
            await pilot.press("ctrl+n")
            await settle()

        async def capture() -> None:
            # Let Textual's brief pressed state end before exporting a stable frame.
            await pilot.pause(0.3)
            shots.append(app.export_screenshot())

        for service in TOKENS:
            app.query_one(f"#url-{service}", Input).value = URLS[service]
        await press("#check")
        await wait_until(lambda: len(app.draft.probes) == 3)
        await capture()

        await advance()
        for service, token in TOKENS.items():
            app.query_one(f"#token-{service}", Input).value = token
            await press(f"#login-button-{service}")
            await wait_until(partial(has_login, service))
        await capture()

        await advance()
        await wait_until(lambda: app.query_one("#project-list", SelectionList).option_count > 0)
        await wait_until(lambda: app.query_one("#repo-list", SelectionList).option_count > 0)
        search = app.query_one("#project-search", Input)
        search.value = "proj"
        search.focus()
        await pilot.press("enter", "space")
        # Pick a repository the way a person does, so the status line follows the toggle.
        repos = app.query_one("#repo-list", SelectionList)
        repos.focus()
        repos.highlighted = 0
        await pilot.press("space")
        search.focus()
        await settle()
        await capture()

        await advance()
        app.query_one("#alias-field", Select).value = "Story Points"
        app.query_one("#alias-name", Input).value = "points"
        await press("#add-alias")
        app.query_one("#move-from", Select).value = "To Do"
        app.query_one("#move-to", Select).value = "In Progress"
        app.query_one("#move-field", Select).value = "Story Points"
        await press("#add-transition")
        app.clear_notifications()
        await settle()
        await capture()

        await advance()
        await wait_until(lambda: app.query_one("#step-5", ReviewStep).candidate is not None)
        await capture()
        await press("#save")
        await wait_until(lambda: app.draft.saved is not None)
        app.clear_notifications()

        await advance()
        app.query_one("#agent-targets", SelectionList).select("agents")
        await press("#install")
        await wait_until(lambda: bool(app.draft.agent_files))
        app.set_focus(None)
        await settle()
        await capture()

        await advance()
        await press("#run-try")
        await wait_until(
            lambda: (
                "It works" in str(app.query_one("#try-status").render())
                and app.query_one("#finish-message").display
                and app.query_one("#try-next").display
            )
        )
        await capture()
    return shots


def build() -> dict[Path, str]:
    outputs: dict[Path, str] = {}
    # Captures always show the chosen theme, regardless of the caller's terminal setting.
    original_no_color = os.environ.pop("NO_COLOR", None)
    try:
        for name, theme in THEMES.items():
            with tempfile.TemporaryDirectory() as tmp:
                shots = asyncio.run(_capture(theme, Path(tmp).resolve()))
            if len(shots) != 7:
                raise RuntimeError(f"expected 7 captures for the {name} theme, got {len(shots)}")
            for number, svg in enumerate(shots, 1):
                # Textual emits an indented blank line between SVG elements.
                svg = re.sub(r"(?m)^[ \t]+$", "", svg)
                leaked = [service for service, token in TOKENS.items() if token in svg]
                if leaked or tmp in svg:
                    raise RuntimeError(f"step {number} ({name}) shows a token or a temporary path")
                outputs[OUT / f"step-{number}-{name}.svg"] = svg
    finally:
        if original_no_color is not None:
            os.environ["NO_COLOR"] = original_no_color
    return outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="regenerate the captures")
    mode.add_argument("--check", action="store_true", help="fail if committed captures drift")
    args = parser.parse_args(argv)
    outputs = build()
    if args.check:
        stale = [
            path.relative_to(ROOT).as_posix()
            for path, svg in outputs.items()
            if not path.is_file() or path.read_text(encoding="utf-8") != svg
        ]
        if stale:
            sys.stderr.write(
                "Stale setup captures: " + ", ".join(stale) + ". Run "
                "`python scripts/capture_setup.py --write` and review the result.\n"
            )
            return 1
        sys.stdout.write("setup captures are up to date\n")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for path, svg in outputs.items():
        path.write_text(svg, encoding="utf-8", newline="\n")
        sys.stdout.write(f"wrote {path.relative_to(ROOT).as_posix()}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
