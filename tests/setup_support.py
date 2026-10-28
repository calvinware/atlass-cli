"""Fixture server routes and helpers for the setup app (shared by tests and the capture script)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.conftest import (
    BITBUCKET,
    CONFLUENCE,
    JIRA,
    FakeKeyring,
    FakeServer,
    bitbucket_path,
    confluence_path,
    fixture,
    jira_path,
)
from yojo.auth import CredentialStore
from yojo.cli import Runtime

URLS = {"jira": JIRA, "confluence": CONFLUENCE, "bitbucket": BITBUCKET}
PROJECTS = [
    {"key": "PROJ", "name": "Example Project"},
    {"key": "EXAMPLE", "name": "Example Team"},
    {"key": "DOCS", "name": "Documentation"},
    {"key": "OPS", "name": "Operations"},
]
STATUSES = [
    {"name": "Bug", "statuses": [{"name": "To Do"}, {"name": "In Progress"}, {"name": "Done"}]},
    {"name": "Task", "statuses": [{"name": "To Do"}, {"name": "Done"}]},
]
REPOS = [("PROJ", "web"), ("PROJ", "api"), ("OPS", "deploy")]


def bitbucket_page(values: list[Any], start: int = 0, last: bool = True) -> dict[str, Any]:
    page: dict[str, Any] = {
        "values": values,
        "start": start,
        "size": len(values),
        "limit": 100,
        "isLastPage": last,
    }
    if not last:
        page["nextPageStart"] = start + len(values)
    return page


def repo(project: str, slug: str) -> dict[str, Any]:
    raw = fixture("bitbucket_repo.json")
    raw["slug"] = raw["name"] = slug
    raw["project"]["key"] = project
    return dict(raw)


def add_routes(server: FakeServer, *, jira_user: str = "example-user") -> FakeServer:
    """A Jira, Confluence, and Bitbucket that answer every read the setup app makes."""
    server.add("GET", jira_path("/rest/api/2/serverInfo"), {"version": "9.12.4"})
    server.add("GET", jira_path("/rest/api/2/myself"), {"name": jira_user})
    server.add("GET", jira_path("/rest/api/2/project"), PROJECTS)
    for project in PROJECTS:
        key = project["key"]
        server.add("GET", jira_path(f"/rest/api/2/project/{key}"), {**project, "id": "1"})
        server.add("GET", jira_path(f"/rest/api/2/project/{key}/statuses"), STATUSES)
        server.add(
            "GET",
            jira_path(f"/rest/api/2/issue/createmeta/{key}/issuetypes"),
            fixture("createmeta_issuetypes.json"),
        )
    server.add("GET", jira_path("/rest/api/2/field"), fixture("fields.json"))
    server.add(
        "GET",
        jira_path("/rest/api/2/search"),
        {
            "startAt": 0,
            "maxResults": 5,
            "total": 2,
            "issues": [
                {
                    "key": "PROJ-2",
                    "fields": {"summary": "Update the example page", "status": {"name": "To Do"}},
                },
                {
                    "key": "PROJ-1",
                    "fields": {"summary": "Set up yojo", "status": {"name": "Done"}},
                },
            ],
        },
    )
    server.add(
        "GET",
        confluence_path("/rest/applinks/1.0/manifest"),
        {"typeId": "confluence", "version": "8.5.4", "name": "Confluence"},
    )
    server.add("GET", confluence_path("/rest/api/user/current"), {"username": jira_user})
    server.add(
        "GET",
        bitbucket_path("/application-properties"),
        fixture("bitbucket_application_properties.json"),
    )
    server.add("GET", bitbucket_path("/profile/recent/repos"), bitbucket_page([]))
    server.add("GET", bitbucket_path("/repos"), bitbucket_page([repo(p, s) for p, s in REPOS]))
    for owner, slug in REPOS:
        server.add("GET", bitbucket_path(f"/projects/{owner}/repos/{slug}"), repo(owner, slug))
    server.add(
        "GET",
        bitbucket_path("/projects/PROJ/repos/web/pull-requests"),
        bitbucket_page([fixture("bitbucket_pull_request.json")]),
    )
    return server


def runtime(cwd: Path, server: FakeServer, keyring: FakeKeyring) -> Runtime:
    import io

    return Runtime(
        stdin=io.BytesIO(),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        cwd=cwd,
        stdin_isatty=lambda: True,
        read_hidden=lambda: "",
        store_factory=lambda: CredentialStore(keyring),
        transport=server.transport(),
        stdout_isatty=lambda: True,
        environ={},
        json_mode=False,
    )


def dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True)
