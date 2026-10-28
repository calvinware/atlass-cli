from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import FakeServer, fixture, jira_path
from yojo.auth import Credential
from yojo.config import FieldRef
from yojo.contracts import YojoError
from yojo.fields import FieldMeta, FieldProblem, coerce, resolve
from yojo.resolve import Candidate, Unresolved, pick, suggest, user_lookup
from yojo.retry import Edit, apply
from yojo.transport import ServiceClient

POOL = [
    Candidate("In Progress", "3", names=("Start Progress",)),
    Candidate("Done", "5"),
    Candidate("Cancelled", "6"),
]


def test_pick_is_case_insensitive_and_accepts_alternate_names() -> None:
    assert pick("in progress", POOL, "status").value == "3"
    assert pick(" START progress ", POOL, "status").value == "3"


def test_pick_prefers_a_unique_exact_case_match() -> None:
    pool = [Candidate("Owner", 1), Candidate("owner", 2)]
    assert pick("owner", pool, "field").value == 2
    with pytest.raises(Unresolved) as info:
        pick("OWNER", pool, "field")
    assert info.value.kind == "ambiguous"
    assert info.value.details()["candidates"] == ["Owner", "owner"]
    assert "matches 2" in info.value.message


def test_pick_suggests_only_a_clear_winner() -> None:
    with pytest.raises(Unresolved) as info:
        pick("Don", POOL, "status")
    assert info.value.suggestion is not None and info.value.suggestion.label == "Done"
    assert info.value.replacement == "Done"
    assert "Did you mean 'Done'?" in info.value.message
    with pytest.raises(Unresolved) as info:
        pick("Archived", POOL, "status")
    assert info.value.suggestion is None
    assert "'In Progress', 'Done', 'Cancelled'" in info.value.fix


def test_suggest_refuses_a_near_tie() -> None:
    pool = [Candidate("Story Points", 1), Candidate("Story Pointz", 2)]
    assert suggest("Story Point", pool) is None


def _meta(field_id: str, schema: dict[str, Any], **extra: Any) -> FieldMeta:
    return FieldMeta.parse(
        field_id,
        {"name": extra.pop("name", field_id), "schema": schema, "required": False, **extra},
    )


def test_field_resolution_order_id_alias_name() -> None:
    pool = {
        "customfield_1": _meta("customfield_1", {"type": "string"}, name="Team"),
        "customfield_2": _meta("customfield_2", {"type": "string"}, name="Squad"),
    }
    aliases = {
        "team": FieldRef("team", None, "customfield_2"),
        "squad_name": FieldRef("squad_name", "Squad", None),
    }
    assert resolve("CUSTOMFIELD_1", pool, aliases).id == "customfield_1"
    assert resolve("team", pool, aliases).id == "customfield_2"
    assert resolve("squad_name", pool, aliases).id == "customfield_2"
    assert resolve("Team", pool, {}).id == "customfield_1"


@pytest.mark.parametrize(
    ("schema", "extra", "given", "expected"),
    [
        ({"type": "number"}, {}, "3", 3),
        ({"type": "number"}, {}, "2.5", 2.5),
        ({"type": "string"}, {}, "  keep spaces ", "  keep spaces "),
        ({"type": "date"}, {}, "2026-10-24", "2026-10-24"),
        ({"type": "array", "items": "string"}, {}, "a, b,,c", ["a", "b", "c"]),
        ({"type": "array", "items": "string"}, {}, ["x"], ["x"]),
        (
            {"type": "option"},
            {"allowedValues": [{"id": "1", "value": "Yes"}, {"id": "2", "value": "No"}]},
            "yes",
            {"id": "1"},
        ),
        (
            {"type": "array", "items": "version"},
            {"allowedValues": [{"id": "10", "name": "1.0"}, {"id": "11", "name": "2.0"}]},
            "2.0, 1.0",
            [{"id": "11"}, {"id": "10"}],
        ),
    ],
)
def test_coerce_plain_text_values(
    schema: dict[str, Any], extra: dict[str, Any], given: Any, expected: Any
) -> None:
    meta = _meta("f", schema, **extra)
    assert coerce(meta, given, lambda name: {"name": name}) == expected


@pytest.mark.parametrize(
    ("schema", "given"),
    [
        ({"type": "number"}, "three"),
        ({"type": "date"}, "24/10/2026"),
        ({"type": "attachment"}, "x"),
        ({"type": "any", "custom": "com.example:unknown-type"}, "x"),
    ],
)
def test_coerce_refuses_what_it_cannot_send_safely(schema: dict[str, Any], given: Any) -> None:
    with pytest.raises(FieldProblem) as info:
        coerce(_meta("f", schema), given, lambda name: {"name": name})
    assert info.value.status == "fail"
    assert info.value.fix


def test_option_without_allowed_values_is_unverified() -> None:
    with pytest.raises(FieldProblem) as info:
        coerce(_meta("f", {"type": "option"}), "Yes", lambda name: {"name": name})
    assert info.value.status == "unverified"


def _users(server: FakeServer) -> Any:
    client = ServiceClient(
        "jira",
        "https://jira.example.test/jira",
        Credential(auth="pat", secret="t"),
        transport=server.transport(),
    )
    return user_lookup(client)


def test_user_lookup_exact_match_and_cache() -> None:
    server = FakeServer().add(
        "GET",
        jira_path("/rest/api/2/user/search"),
        [{"name": "builder1", "displayName": "Example Builder", "emailAddress": "b1@example.com"}],
    )
    lookup = _users(server)
    assert lookup("B1@EXAMPLE.COM") == {"name": "builder1"}
    assert lookup("b1@example.com") == {"name": "builder1"}
    assert len(server.requests) == 1
    assert server.requests[0].url.params["maxResults"] == "10"


def test_user_lookup_single_partial_hit_is_only_a_suggestion() -> None:
    server = FakeServer().add(
        "GET",
        jira_path("/rest/api/2/user/search"),
        [{"name": "builder1", "displayName": "Example Builder"}],
    )
    with pytest.raises(Unresolved) as info:
        _users(server)("build")
    assert info.value.replacement == "builder1"


def test_user_lookup_rejects_malformed_responses() -> None:
    server = FakeServer().add("GET", jira_path("/rest/api/2/user/search"), {"users": []})
    with pytest.raises(YojoError) as info:
        _users(server)("x")
    assert info.value.code == "response_invalid"


# --------------------------------------------------------------------------- retry


BASE = ["jira", "issue", "create", "EXAMPLE", "--set", "Severty=major", "--set=Labels=a"]


def test_retry_edits_apply_to_the_callers_argv() -> None:
    edits = [
        Edit("rename", "severty", "Severity"),
        Edit("drop", "labels"),
        Edit("set", "Environment", "<value>", "missing"),
        Edit("flag", "--type", "Bug"),
    ]
    assert apply(BASE, edits) == [
        "yojo",
        "jira",
        "issue",
        "create",
        "EXAMPLE",
        "--set",
        "Severity=major",
        "--set",
        "Environment=<value>",
        "--type",
        "Bug",
    ]


def test_retry_missing_set_never_overwrites_a_supplied_value() -> None:
    edits = [Edit("rename", "Severty", "Severity"), Edit("set", "Severity", "<value>", "missing")]
    assert apply(BASE, edits) == ["yojo", *BASE[:5], "Severity=major", "--set=Labels=a"]


def test_retry_replaces_a_bad_value_and_keeps_the_equals_form() -> None:
    fixed = apply(BASE, [Edit("set", "labels", "<value>")])
    assert fixed is not None and fixed[-1] == "--set=Labels=<value>"


def test_retry_inserts_before_a_double_dash() -> None:
    argv = ["jira", "issue", "get", "--", "EXAMPLE-1"]
    assert apply(argv, [Edit("flag", "--fields", "x")]) == [
        "yojo",
        "jira",
        "issue",
        "get",
        "--fields",
        "x",
        "--",
        "EXAMPLE-1",
    ]


def test_no_retry_for_values_from_input_or_unknown_edits() -> None:
    assert apply(BASE, [Edit("rename", "x", "y", "input")]) is None
    assert apply(BASE, [Edit("rename", "not-there", "y")]) is None
    assert apply(BASE, [Edit("explode", "x")]) is None


def test_fixture_fields_are_known() -> None:
    names = {f["name"] for f in fixture("fields.json")}
    assert {"Severity", "Story Points", "Linked Issues"} <= names
