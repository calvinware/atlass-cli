"""The setup app, driven with Textual's pilot against the fixture server and a fake keyring."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Checkbox, Input, Select, SelectionList, Static

from tests.conftest import (
    BITBUCKET_TOKEN,
    JIRA_TOKEN,
    FakeKeyring,
    FakeServer,
    bitbucket_path,
    confluence_path,
    jira_path,
)
from tests.setup_support import URLS, add_routes, bitbucket_page, repo, runtime
from yojo import metadata, setup
from yojo.auth import Credential, CredentialStore
from yojo.contracts import COMMANDS_BY_PATH, YojoError, command_schema
from yojo.setup import Draft, Rule, Services, SetupApp

TOKENS = (JIRA_TOKEN, BITBUCKET_TOKEN, "confluence-secret-token-456")
Scenario = Callable[[Pilot[Any]], Awaitable[None]]


class World:
    def __init__(self, tmp_path: Path, server: FakeServer | None = None) -> None:
        self.cwd = tmp_path
        self.server = server or add_routes(FakeServer())
        self.keyring = FakeKeyring()
        self.rt = runtime(tmp_path, self.server, self.keyring)
        self.shots: list[str] = []

    def app(self, draft: Draft | None = None, theme: str | None = None) -> SetupApp:
        return SetupApp(Services(self.rt), draft, theme)

    def drive(
        self,
        scenario: Scenario,
        app: SetupApp | None = None,
        size: tuple[int, int] = (100, 40),
    ) -> SetupApp:
        app = app or self.app()

        async def main() -> None:
            async with app.run_test(size=size) as pilot:
                await scenario(pilot)

        asyncio.run(main())
        return app

    def remember(self, service: str = "jira", token: str = JIRA_TOKEN) -> None:
        CredentialStore(self.keyring).save(service, URLS[service], Credential("pat", token))

    def stored(self, service: str) -> str | None:
        return self.keyring.get_password("yojo", f"{service}|{URLS[service]}")


async def settle(pilot: Pilot[Any]) -> None:
    # Let the button event schedule its worker before waiting for completion.
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause(0.05)


def app_of(pilot: Pilot[Any]) -> SetupApp:
    app = pilot.app
    assert isinstance(app, SetupApp)
    return app


def text(pilot: Pilot[Any], selector: str) -> str:
    return str(pilot.app.query_one(selector, Static).render())


async def press_button(pilot: Pilot[Any], selector: str) -> None:
    pilot.app.query_one(selector, Button).press()
    await settle(pilot)


async def connect(pilot: Pilot[Any], *services: str, **urls: str) -> None:
    for service in services:
        pilot.app.query_one(f"#url-{service}", Input).value = urls.get(service, URLS[service])
    await press_button(pilot, "#check")


def test_connection_result_is_ignored_after_url_or_ca_change(tmp_path: Path) -> None:
    world = World(tmp_path)

    async def scenario(pilot: Pilot[Any]) -> None:
        await connect(pilot, "jira")
        step = pilot.app.query_one("#step-1", setup.ConnectStep)
        old = app_of(pilot).draft.probes["jira"]

        pilot.app.query_one("#url-jira", Input).value = "https://changed.example.test/jira"
        await pilot.pause()
        step._probed("jira", URLS["jira"], None, old, None)
        assert "jira" not in app_of(pilot).draft.probes
        assert step.blocker() is not None

        pilot.app.query_one("#url-jira", Input).value = URLS["jira"]
        await press_button(pilot, "#check")
        pilot.app.query_one("#ca-bundle", Input).value = "new-ca.pem"
        await pilot.pause()
        step._probed("jira", URLS["jira"], None, old, None)
        assert step.blocker() == "The CA bundle changed. Check the connection again."

    world.drive(scenario)


async def next_step(pilot: Pilot[Any]) -> None:
    await pilot.press("ctrl+n")
    await settle(pilot)


async def login(pilot: Pilot[Any], service: str, token: str) -> None:
    pilot.app.query_one(f"#token-{service}", Input).value = token
    await press_button(pilot, f"#login-button-{service}")


async def through_login(pilot: Pilot[Any], *services: str) -> None:
    tokens = {"jira": JIRA_TOKEN, "bitbucket": BITBUCKET_TOKEN, "confluence": TOKENS[2]}
    await connect(pilot, *services)
    await next_step(pilot)
    for service in services:
        await login(pilot, service, tokens[service])
    await next_step(pilot)


def pick(pilot: Pilot[Any], selector: str, value: str) -> None:
    pilot.app.query_one(selector, Select).value = value


def shoot(world: World, pilot: Pilot[Any]) -> None:
    world.shots.append(pilot.app.export_screenshot())


def assert_no_secret(world: World, *extra: str) -> None:
    surfaces = [*world.shots, *extra]
    for path in world.cwd.rglob("*"):
        if path.is_file():
            surfaces.append(path.read_text(errors="replace"))
    for request in world.server.requests:
        surfaces.append(str(request.url))
        surfaces.append(request.content.decode(errors="replace"))
    for token in TOKENS:
        for surface in surfaces:
            assert token not in surface


# --------------------------------------------------------------------------- whole flow


def test_seven_steps_end_to_end_write_only_the_reviewed_file(tmp_path: Path) -> None:
    world = World(tmp_path)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Pilot[Any]) -> None:
        app = app_of(pilot)
        assert "Step 1 of 7" in text(pilot, "#step-title")
        await connect(pilot, "jira", "confluence", "bitbucket")
        assert "Jira 9.12.4" in text(pilot, "#probe-jira")
        assert "Confluence 8.5.4" in text(pilot, "#probe-confluence")
        assert "Bitbucket 8.19.1" in text(pilot, "#probe-bitbucket")
        shoot(world, pilot)
        await next_step(pilot)
        assert app.step == 2
        for service, token in (
            ("jira", JIRA_TOKEN),
            ("confluence", TOKENS[2]),
            ("bitbucket", BITBUCKET_TOKEN),
        ):
            await login(pilot, service, token)
            assert pilot.app.query_one(f"#token-{service}", Input).value == ""
        assert "as example-user" in text(pilot, "#who-jira")
        shoot(world, pilot)
        await next_step(pilot)
        assert app.step == 3
        projects = pilot.app.query_one("#project-search", Input)
        projects.value = "example proj"
        await pilot.pause()
        assert pilot.app.query_one("#project-list", SelectionList).option_count == 1
        projects.focus()
        await pilot.press("enter", "space")
        repos = pilot.app.query_one("#repo-search", Input)
        repos.value = "web"
        repos.focus()
        await pilot.press("enter", "space")
        assert app.draft.projects == ["PROJ"]
        assert app.draft.repos == ["PROJ/web"]
        shoot(world, pilot)
        await next_step(pilot)
        assert app.step == 4
        await settle(pilot)
        pick(pilot, "#alias-field", "Story Points")
        pilot.app.query_one("#alias-name", Input).value = "points"
        await press_button(pilot, "#add-alias")
        pick(pilot, "#move-from", "To Do")
        pick(pilot, "#move-to", "In Progress")
        pick(pilot, "#move-field", "Story Points")
        await press_button(pilot, "#add-transition")
        shoot(world, pilot)
        await next_step(pilot)
        assert app.step == 5
        assert "does not exist yet" in text(pilot, "#review-status")
        diff = text(pilot, "#diff")
        assert "+  fields:" in diff and "points:" in diff
        shoot(world, pilot)
        review = app.query_one("#step-5", setup.ReviewStep)
        assert review.candidate is not None
        seen["reviewed"] = review.candidate.data
        await press_button(pilot, "#save")
        assert "Created ./yojo.yaml" in text(pilot, "#review-status")
        await next_step(pilot)
        assert app.step == 6
        targets = pilot.app.query_one("#agent-targets", SelectionList)
        targets.select("agents")
        await press_button(pilot, "#install")
        assert "AGENTS.md: created" in text(pilot, "#agent-status")
        shoot(world, pilot)
        await next_step(pilot)
        assert app.step == 7
        assert "project = PROJ" in text(pilot, "#try-command")
        await press_button(pilot, "#run-try")
        assert "It works: 2 result(s)" in text(pilot, "#try-status")
        shoot(world, pilot)
        await pilot.press("ctrl+n")

    app = world.drive(scenario)
    saved = (tmp_path / "yojo.yaml").read_bytes()
    assert saved == seen["reviewed"]
    assert b"points:\n      jira_name: Story Points" in saved or b"jira_name: Story Points" in saved
    assert b"to_in_progress" in saved and b"field_set" in saved
    assert (tmp_path / "AGENTS.md").is_file()
    assert world.server.writes() == []
    result = app.return_value
    assert result is not None and result["status"] == "completed"
    assert result["config"] == {"path": str(tmp_path / "yojo.yaml"), "action": "created"}
    assert result["logins_stored"] == ["jira", "confluence", "bitbucket"]
    assert result["agent_files"] == [{"target": "agents", "path": "AGENTS.md", "action": "created"}]
    schema = command_schema(COMMANDS_BY_PATH["setup"])["success"]["properties"]["data"]
    from jsonschema import Draft202012Validator

    Draft202012Validator(schema).validate(result)
    for service in ("jira", "confluence", "bitbucket"):
        assert world.stored(service) is not None
    assert_no_secret(world, json.dumps(result))


# --------------------------------------------------------------------------- step 1


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://jira.example.test/jira", "must use https"),
        ("https://user:pw@jira.example.test", "Not connected"),
    ],
)
def test_connect_refuses_bad_addresses_and_blocks_next(
    tmp_path: Path, url: str, expected: str
) -> None:
    world = World(tmp_path)

    async def scenario(pilot: Pilot[Any]) -> None:
        await connect(pilot, "jira", jira=url)
        status = text(pilot, "#probe-jira")
        assert "Not connected" in status and ("usage_error" in status or expected in status)
        await next_step(pilot)
        assert app_of(pilot).step == 1
        assert "Check the connection" in text(pilot, "#nav-hint")

    world.drive(scenario)
    assert world.server.requests == []


def test_connect_reports_unreachable_and_recovers_on_retry(tmp_path: Path) -> None:
    calls = {"n": 0}
    server = add_routes(FakeServer())

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"version": "9.12.4"})

    server.routes[("GET", jira_path("/rest/api/2/serverInfo"))] = [flaky]
    world = World(tmp_path, server)

    async def scenario(pilot: Pilot[Any]) -> None:
        await connect(pilot, "jira")
        assert "Not connected" in text(pilot, "#probe-jira")
        assert app_of(pilot).draft.probes == {}
        await press_button(pilot, "#check")
        assert "✓ Jira 9.12.4" in text(pilot, "#probe-jira")
        await next_step(pilot)
        assert app_of(pilot).step == 2

    world.drive(scenario)


def test_unreported_version_is_reachable_not_verified(tmp_path: Path) -> None:
    server = add_routes(FakeServer())
    server.routes.pop(("GET", jira_path("/rest/api/2/serverInfo")))
    server.routes.pop(("GET", confluence_path("/rest/applinks/1.0/manifest")))
    world = World(tmp_path, server)

    async def scenario(pilot: Pilot[Any]) -> None:
        await connect(pilot, "jira", "confluence")
        for service in ("jira", "confluence"):
            status = text(pilot, f"#probe-{service}")
            assert status.startswith("✓") and "version unavailable" in status
        assert all(p.version is None for p in app_of(pilot).draft.probes.values())
        await next_step(pilot)
        assert app_of(pilot).step == 2

    world.drive(scenario)


def test_a_server_that_is_not_bitbucket_is_refused(tmp_path: Path) -> None:
    server = add_routes(FakeServer())
    server.routes[("GET", bitbucket_path("/application-properties"))] = []
    server.add("GET", bitbucket_path("/application-properties"), {"displayName": "Other"})
    world = World(tmp_path, server)

    async def scenario(pilot: Pilot[Any]) -> None:
        await connect(pilot, "bitbucket")
        assert "response_invalid" in text(pilot, "#probe-bitbucket")
        await next_step(pilot)
        assert app_of(pilot).step == 1

    world.drive(scenario)


# --------------------------------------------------------------------------- step 2


def test_rejected_token_stores_nothing_and_clears_the_field(tmp_path: Path) -> None:
    server = add_routes(FakeServer())
    server.routes[("GET", jira_path("/rest/api/2/myself"))] = []
    server.add("GET", jira_path("/rest/api/2/myself"), {"errorMessages": ["no"]}, status=401)
    world = World(tmp_path, server)

    async def scenario(pilot: Pilot[Any]) -> None:
        await connect(pilot, "jira")
        await next_step(pilot)
        await login(pilot, "jira", JIRA_TOKEN)
        assert pilot.app.query_one("#token-jira", Input).value == ""
        assert text(pilot, "#who-jira").startswith("Nothing was stored.")
        shoot(world, pilot)
        await next_step(pilot)
        assert app_of(pilot).step == 2
        assert "Log in to Jira first" in text(pilot, "#nav-hint")

    world.drive(scenario)
    assert world.stored("jira") is None
    assert_no_secret(world)


def test_second_login_cannot_race_the_first_keyring_write(tmp_path: Path) -> None:
    world = World(tmp_path)
    app = world.app()
    started = threading.Event()
    release = threading.Event()
    submissions: list[str] = []

    def slow_login(draft: Draft, service: str, secret: str) -> str:
        submissions.append(secret)
        started.set()
        assert release.wait(5)
        CredentialStore(world.keyring).save(service, draft.urls[service], Credential("pat", secret))
        return "Logged in to Jira as example-user."

    app.services.login = slow_login  # type: ignore[method-assign]

    async def scenario(pilot: Pilot[Any]) -> None:
        await connect(pilot, "jira")
        await next_step(pilot)
        step = app.query_one("#step-2", setup.LoginStep)
        box = app.query_one("#token-jira", Input)
        box.value = JIRA_TOKEN
        step._login("jira")
        await pilot.pause()
        assert await asyncio.to_thread(started.wait, 5)
        box.value = "second-fixture-token"
        step._login("jira")
        assert box.value == ""
        assert submissions == [JIRA_TOKEN]
        assert "still in progress" in text(pilot, "#who-jira")
        app.action_next_step()
        assert app.step == 2
        await pilot.press("ctrl+q")
        assert not isinstance(app.screen, setup.LeaveScreen)
        release.set()
        await settle(pilot)
        assert not app.query_one("#login-button-jira", Button).disabled

    try:
        world.drive(scenario, app)
    finally:
        release.set()
    assert submissions == [JIRA_TOKEN]
    assert JIRA_TOKEN in (world.stored("jira") or "")


def test_token_field_never_copies_its_value_out(tmp_path: Path) -> None:
    world = World(tmp_path)

    async def scenario(pilot: Pilot[Any]) -> None:
        await connect(pilot, "jira")
        await next_step(pilot)
        box = pilot.app.query_one("#token-jira", Input)
        box.focus()
        await pilot.press(*JIRA_TOKEN)
        box.select_all()
        await pilot.press("ctrl+x")
        box.select_all()
        box.action_copy()
        await pilot.pause()
        assert JIRA_TOKEN not in pilot.app.clipboard
        assert box.value == JIRA_TOKEN
        shoot(world, pilot)

    world.drive(scenario)
    assert_no_secret(world)


def test_a_saved_login_is_recognized_without_a_token(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.remember()

    async def scenario(pilot: Pilot[Any]) -> None:
        await connect(pilot, "jira")
        await next_step(pilot)
        assert "(saved login)" in text(pilot, "#who-jira")
        await next_step(pilot)
        assert app_of(pilot).step == 3
        assert app_of(pilot).draft.stored == []

    world.drive(scenario)


def test_old_login_result_cannot_authorize_a_new_address(tmp_path: Path) -> None:
    world = World(tmp_path)

    async def scenario(pilot: Pilot[Any]) -> None:
        await connect(pilot, "jira")
        await next_step(pilot)
        app = app_of(pilot)
        step = app.query_one("#step-2", setup.LoginStep)
        app.draft.urls["jira"] = "https://changed.example.test/jira"
        step._stored("jira", URLS["jira"], None, "Previous saved login", None)
        assert "jira" not in app.draft.logins
        step._logged_in("jira", URLS["jira"], None, "Previous token accepted", None)
        assert "jira" not in app.draft.logins
        assert app.draft.stored == ["jira"]
        assert "previous address" in text(pilot, "#who-jira")

    world.drive(scenario)


def test_keyring_failure_is_explained_and_nothing_is_stored(tmp_path: Path) -> None:
    world = World(tmp_path)

    def broken() -> CredentialStore:
        raise YojoError(
            "keyring_unavailable",
            "No secure OS keyring backend is available.",
            "Use a native OS keyring.",
        )

    world.rt.store_factory = broken

    async def scenario(pilot: Pilot[Any]) -> None:
        await connect(pilot, "jira")
        await next_step(pilot)
        await login(pilot, "jira", JIRA_TOKEN)
        status = text(pilot, "#who-jira")
        assert "Nothing was stored" in status and "native OS keyring" in status

    world.drive(scenario)
    assert world.keyring.data == {}


# --------------------------------------------------------------------------- step 3


def test_malformed_project_list_is_not_offered_as_a_choice(tmp_path: Path) -> None:
    server = add_routes(FakeServer())
    server.routes[("GET", jira_path("/rest/api/2/project"))] = []
    server.add("GET", jira_path("/rest/api/2/project"), [{"key": "PROJ"}, {"name": "No key"}])
    world = World(tmp_path, server)

    async def scenario(pilot: Pilot[Any]) -> None:
        await through_login(pilot, "jira")
        assert "Could not load projects" in text(pilot, "#project-status")
        assert pilot.app.query_one("#project-list", SelectionList).option_count == 0

    world.drive(scenario)


def test_repositories_page_with_load_more(tmp_path: Path) -> None:
    server = add_routes(FakeServer())
    server.routes[("GET", bitbucket_path("/repos"))] = []
    server.add("GET", bitbucket_path("/repos"), bitbucket_page([repo("PROJ", "web")], last=False))
    server.add("GET", bitbucket_path("/repos"), bitbucket_page([repo("OPS", "deploy")], start=1))
    world = World(tmp_path, server)

    async def scenario(pilot: Pilot[Any]) -> None:
        await through_login(pilot, "bitbucket")
        more = pilot.app.query_one("#more-repos", Button)
        assert not more.disabled
        assert "More are available" in text(pilot, "#repo-status")
        await press_button(pilot, "#more-repos")
        assert pilot.app.query_one("#more-repos", Button).disabled
        assert pilot.app.query_one("#repo-list", SelectionList).option_count == 2

    world.drive(scenario)
    starts = [
        r.url.params["start"]
        for r in world.server.requests
        if r.url.path == bitbucket_path("/repos")
    ]
    assert starts == ["0", "1"]


# --------------------------------------------------------------------------- step 4


def test_rules_use_jira_names_and_explain_pre_edit(tmp_path: Path) -> None:
    world = World(tmp_path)

    async def scenario(pilot: Pilot[Any]) -> None:
        app = app_of(pilot)
        await through_login(pilot, "jira")
        app.draft.projects.append("PROJ")
        await next_step(pilot)
        assert app.draft.policy() is None  # nothing is preselected
        await settle(pilot)
        pick(pilot, "#create-type", "Bug")
        pick(pilot, "#create-field", "customfield_10700")  # name shared with another field
        await press_button(pilot, "#add-create")
        pick(pilot, "#move-from", "To Do")
        pick(pilot, "#move-to", "Done")
        pick(pilot, "#move-set-field", "Review Flag")
        pilot.app.query_one("#move-set-value", Input).value = "Yes"
        pilot.app.query_one("#move-pre-edit", Checkbox).value = True
        await press_button(pilot, "#add-transition")
        rules = [r.describe() for r in app.draft.rules]
        assert rules[0] == "In PROJ, a new Bug must have: customfield_10700."
        assert 'sets Review Flag to "Yes"' in rules[1]
        assert "two writes that are not atomic" in rules[1]
        policy = app.draft.policy()
        assert policy is not None
        project = policy["jira"]["projects"][0]
        assert project["create"] == {"Bug": {"required": ["customfield_10700"]}}
        assert project["transitions"]["to_done"]["pre_edit"] is True

    world.drive(scenario)


def test_rule_descriptions_cover_every_kind() -> None:
    alias = Rule("alias", None, "points", {"id": "customfield_10002"})
    assert "ID customfield_10002" in alias.describe()
    plain = Rule(
        "transition",
        "PROJ",
        "to_done",
        {"from": ["To Do"], "to": "Done", "auto_set": {"Review Flag": "Yes"}},
    )
    assert "refused before anything is written" in plain.describe()
    attach = Rule(
        "transition",
        "PROJ",
        "to_done",
        {
            "from": ["A", "B"],
            "to": "Done",
            "require": [{"kind": "attachment_filename", "pattern": "{key}_Evidence_*"}],
        },
    )
    assert "from A or B to Done requires an attachment named like {key}_Evidence_*" in (
        attach.describe()
    )


def test_live_rule_problems_are_listed_before_save(tmp_path: Path) -> None:
    world = World(tmp_path)
    draft = Draft(
        urls={"jira": URLS["jira"]},
        projects=["PROJ"],
        rules=[Rule("create", "PROJ", "Bug", {"required": ["No Such Field"]})],
    )

    world.remember()

    async def scenario(pilot: Pilot[Any]) -> None:
        app = app_of(pilot)
        app.draft.probes["jira"] = setup.Probe("jira", URLS["jira"], "9.12.4")
        app.draft.logins["jira"] = "Logged in"
        for _ in range(4):
            await next_step(pilot)
        assert app.step == 5
        status = text(pilot, "#review-status")
        assert status.startswith("Cannot save yet.") and "Retry" in status
        assert pilot.app.query_one("#save", Button).disabled

    world.drive(scenario, world.app(draft))
    assert not (tmp_path / "yojo.yaml").exists()


# --------------------------------------------------------------------------- step 5


def _ready(world: World, **extra: Any) -> SetupApp:
    draft = Draft(urls={"jira": URLS["jira"]}, projects=["PROJ"], **extra)
    draft.probes["jira"] = setup.Probe("jira", URLS["jira"], "9.12.4")
    draft.logins["jira"] = "Logged in"
    world.remember()
    return world.app(draft)


async def _to_review(pilot: Pilot[Any]) -> None:
    for _ in range(4):
        await next_step(pilot)
    assert app_of(pilot).step == 5


async def _save_and_continue(pilot: Pilot[Any]) -> None:
    await _to_review(pilot)
    await press_button(pilot, "#save")
    assert app_of(pilot).draft.saved is not None
    await next_step(pilot)
    assert app_of(pilot).step == 6


def test_existing_file_shows_a_diff_and_is_replaced_only_on_save(tmp_path: Path) -> None:
    world = World(tmp_path)
    existing = "version: 1\njira:\n  base_url: https://jira.example.test/jira\n"
    (tmp_path / "yojo.yaml").write_text(existing)
    loaded = setup.draft_from_file(Services(world.rt).existing())
    loaded.probes["jira"] = setup.Probe("jira", URLS["jira"], "9.12.4")
    loaded.logins["jira"] = "Logged in"
    loaded.projects.append("PROJ")
    world.remember()

    async def scenario(pilot: Pilot[Any]) -> None:
        await _to_review(pilot)
        assert "Save replaces ./yojo.yaml" in text(pilot, "#review-status")
        diff = text(pilot, "#diff")
        assert "+  - key: PROJ" in diff
        assert (tmp_path / "yojo.yaml").read_text() == existing
        await press_button(pilot, "#save")
        assert "Saved ./yojo.yaml" in text(pilot, "#review-status")

    world.drive(scenario, world.app(loaded))
    assert "- key: PROJ" in (tmp_path / "yojo.yaml").read_text()


def test_file_changed_after_review_is_not_overwritten(tmp_path: Path) -> None:
    world = World(tmp_path)

    async def scenario(pilot: Pilot[Any]) -> None:
        await _to_review(pilot)
        (tmp_path / "yojo.yaml").write_text(
            "version: 1\nconfluence:\n  base_url: https://c.example.test\n"
        )
        await press_button(pilot, "#save")
        status = text(pilot, "#review-status")
        assert status.startswith("Not saved; nothing was written.")
        assert "file changed after review" in status.lower()
        assert pilot.app.query_one("#save", Button).disabled
        await press_button(pilot, "#preview")
        assert "Save replaces ./yojo.yaml" in text(pilot, "#review-status")
        assert not pilot.app.query_one("#save", Button).disabled

    world.drive(scenario, _ready(world))
    assert "confluence" in (tmp_path / "yojo.yaml").read_text()
    assert [p.name for p in tmp_path.iterdir()] == ["yojo.yaml"]


def test_live_metadata_change_after_review_needs_a_fresh_preview(tmp_path: Path) -> None:
    server = add_routes(FakeServer())
    world = World(tmp_path, server)

    async def scenario(pilot: Pilot[Any]) -> None:
        await _to_review(pilot)
        server.routes[("GET", jira_path("/rest/api/2/project/PROJ"))] = []
        server.add(
            "GET", jira_path("/rest/api/2/project/PROJ"), {"key": "PROJ", "name": "Renamed Project"}
        )
        await press_button(pilot, "#save")
        assert "Live metadata changed" in text(pilot, "#review-status")

    world.drive(scenario, _ready(world))
    assert not (tmp_path / "yojo.yaml").exists()


def test_choices_changed_after_review_invalidate_save(tmp_path: Path) -> None:
    world = World(tmp_path)

    async def scenario(pilot: Pilot[Any]) -> None:
        app = app_of(pilot)
        await _to_review(pilot)
        app.draft.touch()
        await press_button(pilot, "#save")
        assert "changed after this preview" in text(pilot, "#review-status")

    world.drive(scenario, _ready(world))
    assert not (tmp_path / "yojo.yaml").exists()


def test_symlinked_destination_is_refused(tmp_path: Path) -> None:
    world = World(tmp_path)
    target = tmp_path / "elsewhere.yaml"
    target.write_text("version: 1\n")
    try:
        (tmp_path / "linked.yaml").symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are not available")

    async def scenario(pilot: Pilot[Any]) -> None:
        await _to_review(pilot)
        pilot.app.query_one("#output", Input).value = "linked.yaml"
        await press_button(pilot, "#preview")
        assert "file_invalid" in text(pilot, "#review-status")
        assert pilot.app.query_one("#save", Button).disabled

    world.drive(scenario, _ready(world))
    assert target.read_text() == "version: 1\n"


def test_legacy_and_invalid_config_get_their_own_errors(tmp_path: Path) -> None:
    world = World(tmp_path)
    (tmp_path / "atl.yaml").write_text("version: 2\n")
    with pytest.raises(YojoError) as legacy:
        Services(world.rt).existing()
    assert legacy.value.code == "config_legacy"
    (tmp_path / "yojo.yaml").write_text("version: 7\n")
    with pytest.raises(YojoError) as invalid:
        Services(world.rt).existing()
    assert invalid.value.code == "config_invalid"


def test_existing_rules_are_kept_and_dropped_keys_are_named(tmp_path: Path) -> None:
    doc = {
        "version": 1,
        "jira": {
            "base_url": URLS["jira"],
            "max_attachment_bytes": 10,
            "fields": {"points": {"jira_name": "Story Points"}},
            "projects": [{"key": "PROJ", "transitions": {"done": {"from": ["A"], "to": "B"}}}],
        },
    }
    draft = setup.draft_from_file(doc)
    assert draft.projects == ["PROJ"]
    assert {r.origin for r in draft.rules} == {"file"}
    assert draft.policy() == {
        "jira": {
            "fields": {"points": {"jira_name": "Story Points"}},
            "projects": [{"key": "PROJ", "transitions": {"done": {"from": ["A"], "to": "B"}}}],
        }
    }
    assert any("jira.max_attachment_bytes" in note for note in draft.notes)


# --------------------------------------------------------------------------- steps 6 and 7


def test_agent_install_reports_a_partial_write(tmp_path: Path) -> None:
    world = World(tmp_path)
    # Another writer holds GEMINI.md, so AGENTS.md is written first and GEMINI.md is refused.
    (tmp_path / ".GEMINI.md.yojo-lock").write_text("")

    async def scenario(pilot: Pilot[Any]) -> None:
        await _save_and_continue(pilot)
        targets = pilot.app.query_one("#agent-targets", SelectionList)
        await press_button(pilot, "#install")
        assert "Select at least one tool" in text(pilot, "#agent-status")
        targets.select("agents")
        targets.select("gemini")
        await press_button(pilot, "#install")
        status = text(pilot, "#agent-status")
        assert status.startswith("Not everything was written.")
        assert "Written: AGENTS.md (created)" in status
        assert "--target agents --target gemini" in text(pilot, "#agent-command")

    app = world.drive(scenario, _ready(world))
    assert (tmp_path / "AGENTS.md").is_file()
    assert not (tmp_path / "GEMINI.md").exists()
    assert app.draft.agent_files == [{"target": "agents", "path": "AGENTS.md", "action": "created"}]


async def _departures_are_held(pilot: Pilot[Any], step: int, hint: str) -> None:
    """Next, Back, Leave, and Finish all wait while a local write has not reported back."""
    app = app_of(pilot)
    for key in ("ctrl+n", "ctrl+b", "ctrl+q", "escape"):
        await pilot.press(key)
        await pilot.pause()
        assert app.step == step
        assert not isinstance(app.screen, setup.LeaveScreen)
        assert hint in text(pilot, "#nav-hint")
    app.finish("completed")
    await pilot.pause()
    assert app.is_running
    assert app.return_value is None


def test_held_save_keeps_setup_open_until_its_result_is_shown(tmp_path: Path) -> None:
    world = World(tmp_path)
    app = _ready(world)
    started = threading.Event()
    release = threading.Event()
    save = app.services.save

    def slow_save(candidate: Any) -> str:
        started.set()
        assert release.wait(5)
        return save(candidate)

    app.services.save = slow_save  # type: ignore[method-assign]

    async def scenario(pilot: Pilot[Any]) -> None:
        await _to_review(pilot)
        app.query_one("#save", Button).press()
        await pilot.pause()
        assert await asyncio.to_thread(started.wait, 5)
        await _departures_are_held(pilot, 5, "still being saved")
        release.set()
        await settle(pilot)
        assert "Created ./yojo.yaml" in text(pilot, "#review-status")
        await pilot.press("ctrl+q", "tab", "enter")
        await pilot.pause()

    try:
        world.drive(scenario, app)
    finally:
        release.set()
    assert app.return_value is not None
    assert app.return_value["status"] == "cancelled"
    assert app.return_value["config"] == {
        "path": str(tmp_path / "yojo.yaml"),
        "action": "created",
    }
    assert (tmp_path / "yojo.yaml").is_file()


def test_held_agent_install_is_reported_before_setup_finishes(tmp_path: Path) -> None:
    world = World(tmp_path)
    app = _ready(world)
    started = threading.Event()
    release = threading.Event()
    install = app.services.install_agents
    calls: list[list[str]] = []

    def slow_install(targets: list[str]) -> dict[str, Any]:
        calls.append(targets)
        started.set()
        assert release.wait(5)
        return install(targets)

    app.services.install_agents = slow_install  # type: ignore[method-assign]

    async def scenario(pilot: Pilot[Any]) -> None:
        await _save_and_continue(pilot)
        app.query_one("#agent-targets", SelectionList).select("agents")
        app.query_one("#install", Button).press()
        await pilot.pause()
        assert await asyncio.to_thread(started.wait, 5)
        app.query_one("#install", Button).press()
        await pilot.pause()
        assert calls == [["agents"]]
        await _departures_are_held(pilot, 6, "still being written")
        release.set()
        await settle(pilot)
        assert "AGENTS.md: created" in text(pilot, "#agent-status")
        await next_step(pilot)
        await press_button(pilot, "#run-try")
        app.finish("completed")
        await pilot.pause()

    try:
        world.drive(scenario, app)
    finally:
        release.set()
    assert app.return_value is not None
    assert app.return_value["status"] == "completed"
    assert app.return_value["agent_files"] == [
        {"target": "agents", "path": "AGENTS.md", "action": "created"}
    ]
    assert (tmp_path / "AGENTS.md").is_file()


def test_try_shows_a_failed_read_with_its_fix_and_writes_nothing(tmp_path: Path) -> None:
    server = add_routes(FakeServer())
    server.routes[("GET", jira_path("/rest/api/2/search"))] = []
    server.add("GET", jira_path("/rest/api/2/search"), {"errorMessages": ["bad"]}, status=400)
    world = World(tmp_path, server)

    async def scenario(pilot: Pilot[Any]) -> None:
        await _save_and_continue(pilot)
        await next_step(pilot)
        command = text(pilot, "#try-command")
        assert command.startswith("yojo --json jira search") and "--base-url" not in command
        await press_button(pilot, "#run-try")
        status = text(pilot, "#try-status")
        assert status.startswith("✗ Read failed (exit 5).") and "Retry" in status

    world.drive(scenario, _ready(world))
    assert world.server.writes() == []


@pytest.mark.parametrize(
    ("draft", "expected"),
    [
        (
            Draft(urls={"jira": URLS["jira"]}),
            ["jira", "search", "--jql", "assignee = currentUser() ORDER BY updated DESC"],
        ),
        (
            Draft(urls={"bitbucket": URLS["bitbucket"]}, repos=["PROJ/web"]),
            ["bitbucket", "pr", "list", "--repo", "PROJ/web"],
        ),
        (Draft(urls={"bitbucket": URLS["bitbucket"]}), ["bitbucket", "repo", "list"]),
        (
            Draft(urls={"confluence": URLS["confluence"]}),
            ["confluence", "page", "search", "--cql", "type = page"],
        ),
    ],
)
def test_try_command_is_read_only_and_matches_the_context(
    tmp_path: Path, draft: Draft, expected: list[str]
) -> None:
    argv = Services(World(tmp_path).rt).try_argv(draft)
    service = next(iter(draft.urls))
    assert argv == ["yojo", "--json", *expected, "--limit", "5", "--base-url", draft.urls[service]]
    assert COMMANDS_BY_PATH[" ".join(expected[:3] if expected[0] != "jira" else expected[:2])]


# --------------------------------------------------------------------------- navigation


def test_back_keeps_choices_and_cancel_leaves_config_unchanged(tmp_path: Path) -> None:
    world = World(tmp_path)

    async def scenario(pilot: Pilot[Any]) -> None:
        app = app_of(pilot)
        await connect(pilot, "jira")
        await next_step(pilot)
        await login(pilot, "jira", JIRA_TOKEN)
        await pilot.press("ctrl+b")
        await settle(pilot)
        assert app.step == 1
        assert pilot.app.query_one("#url-jira", Input).value == URLS["jira"]
        await next_step(pilot)
        assert "Saved to your keyring" in text(pilot, "#who-jira")
        await pilot.press("escape")
        await pilot.pause()
        message = str(pilot.app.screen.query_one("#leave Static:last-of-type", Static).render())
        shoot(world, pilot)
        assert "yojo.yaml was not changed." in message
        assert "stay in your OS keyring: Jira" in message
        await pilot.press("escape")  # keep going
        await pilot.pause()
        assert not isinstance(pilot.app.screen, setup.LeaveScreen)
        await pilot.press("ctrl+q", "tab", "enter")
        await pilot.pause()

    app = world.drive(scenario)
    assert app.return_value is not None
    assert app.return_value["status"] == "cancelled"
    assert app.return_value["config"] is None
    assert app.return_value["logins_stored"] == ["jira"]
    assert not (tmp_path / "yojo.yaml").exists()
    assert_no_secret(world)


def test_keyboard_only_reaches_save(tmp_path: Path) -> None:
    world = World(tmp_path)

    async def scenario(pilot: Pilot[Any]) -> None:
        app = app_of(pilot)
        assert app.focused is pilot.app.query_one("#url-jira", Input)
        await pilot.press(*URLS["jira"], "enter")
        await settle(pilot)
        await next_step(pilot)
        assert app.focused is pilot.app.query_one("#token-jira", Input)
        await pilot.press(*JIRA_TOKEN, "enter")
        await settle(pilot)
        await next_step(pilot)
        await pilot.press(*"PROJ", "enter", "space")
        assert app.draft.projects == ["PROJ"]
        await next_step(pilot)
        await next_step(pilot)
        assert app.step == 5
        save: object = pilot.app.query_one("#save", Button)
        for _ in range(12):
            if app.focused is save:
                break
            await pilot.press("tab")
        assert app.focused is save
        await pilot.press("enter")
        await settle(pilot)

    world.drive(scenario)
    assert (tmp_path / "yojo.yaml").is_file()
    assert_no_secret(world)


# --------------------------------------------------------------------------- terminals


def test_narrow_terminal_keeps_progress_and_right_aligned_navigation(tmp_path: Path) -> None:
    world = World(tmp_path)

    async def scenario(pilot: Pilot[Any]) -> None:
        app = app_of(pilot)
        assert app.query_one("#progress").display
        assert "Step 1 of 7" in text(pilot, "#step-title")
        assert "Connect" in app.export_screenshot()
        next_button = app.query_one("#next", Button)
        assert not app.query_one("#back", Button).display
        assert next_button.region.x >= 60
        app._show(2)
        await pilot.pause()
        back = app.query_one("#back", Button)
        assert back.display
        assert back.region.x >= 40
        assert back.region.right <= next_button.region.x

    world.drive(scenario, size=(80, 24))


def test_light_dark_toggle_and_no_color(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    world = World(tmp_path)
    seen: dict[str, Any] = {}

    async def scenario(pilot: Pilot[Any]) -> None:
        seen["first"] = pilot.app.current_theme.dark
        await pilot.press("ctrl+t")
        seen["second"] = pilot.app.current_theme.dark
        seen["no_color"] = pilot.app.no_color

    world.drive(scenario, world.app(theme="textual-dark"))
    assert seen == {"first": True, "second": False, "no_color": False}
    monkeypatch.setenv("NO_COLOR", "1")
    world.drive(scenario, world.app(theme="textual-light"))
    assert seen == {"first": False, "second": True, "no_color": True}


# ----------------------------------------------------------------- remote text and captures


def test_remote_text_is_shown_literally_without_escapes() -> None:
    shown = setup.plain("\x1b[31mred\x1b[0m [b]bold[/b]\nsecond line").plain
    assert "\x1b" not in shown
    assert "[b]bold[/b]" in shown
    assert shown.splitlines()[1] == "second line"


CAPTURES = Path(__file__).resolve().parents[1] / "docs" / "assets" / "setup"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_committed_captures_cover_seven_steps_without_secrets(theme: str) -> None:
    for number, _name in enumerate(setup.STEPS, 1):
        svg = (CAPTURES / f"step-{number}-{theme}.svg").read_text(encoding="utf-8")
        assert svg.startswith("<svg")
        title = f"Step {number} of 7".replace(" ", "&#160;")
        assert title in svg
        for token in TOKENS:
            assert token not in svg


@pytest.mark.parametrize(
    "rows",
    [
        [{"id": "customfield_10001", "name": ""}],
        [
            {"id": "customfield_10001", "name": "Points"},
            {"id": "customfield_10001", "name": "Other"},
        ],
    ],
)
def test_incomplete_or_duplicate_field_metadata_is_refused(rows: list[dict[str, str]]) -> None:
    class Client:
        def get(self, path: str) -> list[dict[str, str]]:
            assert path == "/rest/api/2/field"
            return rows

    with pytest.raises(YojoError) as error:
        metadata.all_fields(Client())  # type: ignore[arg-type]
    assert error.value.code == "metadata_incomplete"


def test_failed_fields_have_a_keyboard_retry(tmp_path: Path) -> None:
    world = World(tmp_path)
    app = _ready(world)
    original = app.services.fields
    calls = 0

    def flaky(draft: Draft) -> list[setup.FieldChoice]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise YojoError("response_invalid", "secret-token-123", "secret-token-123")
        return original(draft)

    app.services.fields = flaky  # type: ignore[method-assign]

    async def scenario(pilot: Pilot[Any]) -> None:
        for _ in range(3):
            await pilot.press("ctrl+n")
            await settle(pilot)
        assert app.step == 4
        assert app.query_one("#retry-fields", Button).display
        assert "secret-token-123" not in app.export_screenshot()
        app.query_one("#retry-fields", Button).focus()
        await pilot.press("enter")
        await settle(pilot)
        assert calls == 2
        assert not app.query_one("#retry-fields", Button).display
        assert app.query_one("#step-4", setup.RulesStep).fields

    world.drive(scenario, app)


def test_keyboard_finish_waits_for_saved_config_and_successful_try(tmp_path: Path) -> None:
    world = World(tmp_path)
    app = _ready(world)

    async def scenario(pilot: Pilot[Any]) -> None:
        await _to_review(pilot)
        await pilot.press("ctrl+n")
        assert app.step == 5
        app.finish("completed")
        assert app.is_running
        await press_button(pilot, "#save")
        await pilot.press("ctrl+n", "ctrl+n")
        await settle(pilot)
        assert app.step == 7
        await pilot.press("ctrl+n")
        assert app.is_running
        assert "read-only" in text(pilot, "#nav-hint")
        app.query_one("#run-try", Button).focus()
        await pilot.press("enter")
        await settle(pilot)
        assert app.draft.try_revision == app.draft.saved_revision
        await pilot.press("ctrl+n")

    world.drive(scenario, app)
    assert app.return_value is not None
    assert app.return_value["status"] == "completed"


def test_try_error_does_not_echo_secret_bearing_server_text(tmp_path: Path) -> None:
    world = World(tmp_path)
    app = _ready(world)

    async def scenario(pilot: Pilot[Any]) -> None:
        await _save_and_continue(pilot)
        await next_step(pilot)
        step = app.query_one("#step-7", setup.TryStep)
        step._ran(
            (
                {
                    "ok": False,
                    "error": {"code": "server_rejected", "message": TOKENS[0], "fix": TOKENS[0]},
                },
                5,
            ),
            None,
        )
        assert TOKENS[0] not in app.export_screenshot()
        assert app.draft.try_revision is None

    world.drive(scenario, app)


def test_late_project_discovery_cannot_restore_old_connection_choices(tmp_path: Path) -> None:
    world = World(tmp_path)
    app = _ready(world)
    started = threading.Event()
    release = threading.Event()
    original = app.services.projects

    def slow(draft: Draft) -> list[dict[str, str]]:
        started.set()
        assert release.wait(5)
        return original(draft)

    app.services.projects = slow  # type: ignore[method-assign]

    async def scenario(pilot: Pilot[Any]) -> None:
        await pilot.press("ctrl+n", "ctrl+n")
        await pilot.pause()
        assert await asyncio.to_thread(started.wait, 5)
        await pilot.press("ctrl+b", "ctrl+b")
        app.query_one("#url-jira", Input).value = "https://new.example.test/jira"
        await pilot.pause()
        assert app.draft.projects == []
        release.set()
        await settle(pilot)
        choices = app.query_one("#step-3", setup.ChooseStep)
        assert choices.all_projects == []
        assert app.draft.saved is None

    try:
        world.drive(scenario, app)
    finally:
        release.set()


def test_real_project_picker_change_invalidates_saved_preview_and_try(tmp_path: Path) -> None:
    world = World(tmp_path)
    app = _ready(world)

    async def scenario(pilot: Pilot[Any]) -> None:
        await _save_and_continue(pilot)
        assert app.saved_snapshot_current()
        await pilot.press("ctrl+b", "ctrl+b", "ctrl+b")
        await settle(pilot)
        assert app.step == 3
        search = app.query_one("#project-search", Input)
        search.value = "example proj"
        search.focus()
        await pilot.press("enter", "space")
        await pilot.pause()
        assert app.draft.projects == []
        assert not app.saved_snapshot_current()
        await pilot.press("ctrl+n", "ctrl+n")
        await settle(pilot)
        assert app.step == 5
        assert str(app.query_one("#next", Button).label) == "Save"
        assert app.query_one("#step-5", setup.ReviewStep).blocker() is not None

    world.drive(scenario, app)


def test_leaving_after_a_connection_change_reports_the_earlier_save(tmp_path: Path) -> None:
    world = World(tmp_path)
    app = _ready(world)

    async def scenario(pilot: Pilot[Any]) -> None:
        await _save_and_continue(pilot)
        assert app.saved_snapshot_current()
        await pilot.press("ctrl+b", "ctrl+b", "ctrl+b", "ctrl+b", "ctrl+b")
        assert app.step == 1
        app.query_one("#url-jira", Input).value = "https://new.example.test/jira"
        await pilot.pause()
        assert app.draft.saved is not None
        assert not app.saved_snapshot_current()
        app.finish("cancelled")

    world.drive(scenario, app)
    result = app.return_value
    assert result is not None
    assert result["status"] == "cancelled"
    assert result["config"]["path"] == str(tmp_path / "yojo.yaml")
    assert any("Later choices were not saved" in note for note in result["notes"])
    assert any("review and save later choices" in step for step in result["next"])


def test_edit_during_save_keeps_earlier_file_from_completing_setup(tmp_path: Path) -> None:
    world = World(tmp_path)
    app = _ready(world)
    started = threading.Event()
    release = threading.Event()
    original = app.services.save

    def slow(candidate: Any) -> str:
        started.set()
        assert release.wait(5)
        return original(candidate)

    app.services.save = slow  # type: ignore[method-assign]

    async def scenario(pilot: Pilot[Any]) -> None:
        await _to_review(pilot)
        app.query_one("#save", Button).press()
        await pilot.pause()
        assert await asyncio.to_thread(started.wait, 5)
        app.query_one("#output", Input).value = "new.yaml"
        await pilot.pause()
        release.set()
        await settle(pilot)
        assert (tmp_path / "yojo.yaml").is_file()
        assert app.draft.saved_revision != app.draft.revision
        assert not app.saved_snapshot_current()
        await pilot.press("ctrl+n")
        assert app.step == 5

    try:
        world.drive(scenario, app)
    finally:
        release.set()


def test_late_try_success_cannot_replace_a_newer_failed_retry(tmp_path: Path) -> None:
    world = World(tmp_path)
    app = _ready(world)
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def reads(_argv: list[str]) -> tuple[dict[str, Any], int]:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            assert release.wait(5)
            return {"ok": True, "data": {}}, 0
        return {"ok": False, "error": {"code": "server_error"}}, 5

    app.services.run_read = reads  # type: ignore[assignment]

    async def scenario(pilot: Pilot[Any]) -> None:
        await _save_and_continue(pilot)
        await next_step(pilot)
        app.query_one("#run-try", Button).press()
        await pilot.pause()
        assert await asyncio.to_thread(started.wait, 5)
        app.query_one("#run-try", Button).press()
        await pilot.pause()
        release.set()
        await settle(pilot)
        assert calls == 2
        assert app.draft.try_revision is None
        assert app.query_one("#step-7", setup.TryStep).blocker() is not None

    try:
        world.drive(scenario, app)
    finally:
        release.set()
