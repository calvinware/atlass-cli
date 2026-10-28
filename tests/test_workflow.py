from __future__ import annotations

import copy
import json
import re
import shlex
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import (
    JIRA,
    SHELL_SENTINELS,
    Harness,
    backticked,
    config_text,
    fixture,
    jira_path,
    run_argv,
    run_in_posix_shell,
)
from yojo import transport, workflow

KEY = "EXAMPLE-1"
ISSUE = jira_path(f"/rest/api/2/issue/{KEY}")
TRANSITIONS = jira_path(f"/rest/api/2/issue/{KEY}/transitions")
EDITMETA = jira_path(f"/rest/api/2/issue/{KEY}/editmeta")


def setup(
    harness: Harness,
    *,
    issue: str = "issue_todo.json",
    transitions: Any = "transitions_todo.json",
    pre_edit: str = "false",
    review_flag: str = "Yes",
) -> None:
    harness.write_config(config_text(pre_edit=pre_edit, review_flag=review_flag))
    harness.store()
    harness.server.add("GET", ISSUE, fixture(issue))
    body = fixture(transitions) if isinstance(transitions, str) else transitions
    harness.server.add("GET", TRANSITIONS, body)
    harness.server.add("GET", EDITMETA, fixture("editmeta.json"))


def checks_by_id(checklist: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in checklist["checks"]}


# --------------------------------------------------------------------------- requirements


def test_requirements_ready_with_on_screen_field(harness: Harness) -> None:
    setup(harness)
    result = harness.run("jira", "requirements", KEY, "--to", "In Progress")
    assert result.code == 0
    data = result.data
    assert data["ready"] is True
    assert data["transition"] == {"id": "11", "name": "Start Progress"}
    checks = checks_by_id(data)
    assert checks["auto_set:review_flag"]["details"]["placement"] == "transition"
    assert data["plan"]["atomic"] is True
    assert [op["op"] for op in data["plan"]["operations"]] == ["transition"]
    assert data["plan"]["operations"][0]["fields"][0]["id"] == "customfield_10300"
    assert harness.server.writes() == []
    assert EDITMETA not in harness.server.paths()


def test_requirements_is_case_insensitive_on_status(harness: Harness) -> None:
    setup(harness)
    assert harness.run("jira", "requirements", KEY, "--to", "in progress").data["ready"] is True


def test_requirements_wrong_source_status(harness: Harness) -> None:
    setup(harness, issue="issue_in_progress.json", transitions="transitions_in_progress.json")
    data = harness.run("jira", "requirements", KEY, "--to", "In Progress").data
    assert data["ready"] is False
    check = checks_by_id(data)["source_status"]
    assert check["status"] == "fail"
    assert "'To Do'" in check["fix"]


def test_requirements_transition_unavailable(harness: Harness) -> None:
    setup(harness, transitions={"transitions": []})
    data = harness.run("jira", "requirements", KEY, "--to", "In Progress").data
    checks = checks_by_id(data)
    assert checks["transition_available"]["status"] == "fail"
    assert checks["auto_set:review_flag"]["status"] == "unverified"
    assert data["ready"] is False


def test_requirements_duplicate_live_transitions(harness: Harness) -> None:
    body = fixture("transitions_todo.json")
    dup = copy.deepcopy(body["transitions"][0])
    dup["id"] = "99"
    body["transitions"].append(dup)
    setup(harness, transitions=body)
    check = checks_by_id(harness.run("jira", "requirements", KEY, "--to", "In Progress").data)
    assert check["transition_available"]["status"] == "fail"
    assert check["transition_available"]["details"]["transition_ids"] == ["11", "99"]


def test_requirements_status_not_offered_lists_what_is(harness: Harness) -> None:
    setup(harness)
    data = harness.run("jira", "requirements", KEY, "--to", "Archived").data
    assert data["ready"] is False
    check = checks_by_id(data)["transition_available"]
    assert check["status"] == "fail"
    assert check["details"]["offered"] == ["Cancelled", "In Progress"]
    assert "'In Progress'" in check["fix"]
    assert harness.server.writes() == []


def test_requirements_suggests_a_close_status(harness: Harness) -> None:
    setup(harness)
    check = checks_by_id(harness.run("jira", "requirements", KEY, "--to", "In Progres").data)
    assert check["transition_available"]["details"]["suggestion"] == "In Progress"
    assert "Did you mean 'In Progress'?" in check["transition_available"]["message"]


def test_requirements_matches_the_transition_name(harness: Harness) -> None:
    setup(harness)
    data = harness.run("jira", "requirements", KEY, "--to", "start progress").data
    assert data["ready"] is True
    assert data["to"] == "In Progress"


def test_requirements_without_config_uses_live_checks_only(harness: Harness) -> None:
    harness.store()
    issue = fixture("issue_todo.json")
    issue["key"] = "OTHER-1"
    issue["fields"]["project"] = {"key": "OTHER", "id": "10010"}
    harness.server.add("GET", jira_path("/rest/api/2/issue/OTHER-1"), issue)
    harness.server.add(
        "GET", jira_path("/rest/api/2/issue/OTHER-1/transitions"), fixture("transitions_todo.json")
    )
    result = harness.run("--base-url", JIRA, "jira", "requirements", "OTHER-1", "--to", "Doing")
    assert result.code == 0
    data = result.data
    assert data["rule"] is None and data["ready"] is False
    assert [c["id"] for c in data["checks"]] == ["transition_available"]
    result = harness.run(
        "--base-url", JIRA, "jira", "requirements", "OTHER-1", "--to", "In Progress"
    )
    assert result.data["ready"] is True
    assert result.data["plan"]["operations"][0]["fields"] == []


def test_off_screen_auto_set_without_pre_edit_fails(harness: Harness) -> None:
    setup(harness, transitions="transitions_todo_no_screen.json")
    data = harness.run("jira", "requirements", KEY, "--to", "In Progress").data
    check = checks_by_id(data)["auto_set:review_flag"]
    assert check["status"] == "fail"
    assert "pre_edit: true" in check["fix"]
    assert EDITMETA not in harness.server.paths()


def test_off_screen_auto_set_with_pre_edit_plans_edit(harness: Harness) -> None:
    setup(harness, transitions="transitions_todo_no_screen.json", pre_edit="true")
    data = harness.run("jira", "requirements", KEY, "--to", "In Progress").data
    assert data["ready"] is True
    assert data["plan"]["atomic"] is False
    ops = data["plan"]["operations"]
    assert [(o["op"], o["method"]) for o in ops] == [("edit", "PUT"), ("transition", "POST")]
    assert ops[0]["fields"][0]["id"] == "customfield_10300"
    assert ops[1]["fields"] == []
    assert "non-atomic" in data["notice"]
    assert checks_by_id(data)["auto_set:review_flag"]["details"]["placement"] == "edit"


def test_pre_edit_not_in_editmeta(harness: Harness) -> None:
    setup(harness, transitions="transitions_todo_no_screen.json", pre_edit="true")
    harness.server.routes[("GET", EDITMETA)] = []
    harness.server.add("GET", EDITMETA, {"fields": {}})
    check = checks_by_id(harness.run("jira", "requirements", KEY, "--to", "In Progress").data)
    assert check["auto_set:review_flag"]["status"] == "fail"
    assert (
        "neither on the transition screen nor editable" in check["auto_set:review_flag"]["message"]
    )


def test_pre_edit_without_set_operation(harness: Harness) -> None:
    setup(harness, transitions="transitions_todo_no_screen.json", pre_edit="true")
    meta = fixture("editmeta.json")
    meta["fields"]["customfield_10300"]["operations"] = ["add"]
    harness.server.routes[("GET", EDITMETA)] = []
    harness.server.add("GET", EDITMETA, meta)
    check = checks_by_id(harness.run("jira", "requirements", KEY, "--to", "In Progress").data)
    assert "cannot be set" in check["auto_set:review_flag"]["message"]


def test_disallowed_option_value(harness: Harness) -> None:
    setup(harness, review_flag="Maybe")
    check = checks_by_id(harness.run("jira", "requirements", KEY, "--to", "In Progress").data)
    assert check["auto_set:review_flag"]["status"] == "fail"
    assert "'Yes', 'No'" in check["auto_set:review_flag"]["fix"]


def test_disabled_option_is_not_allowed(harness: Harness) -> None:
    body = fixture("transitions_todo.json")
    body["transitions"][0]["fields"]["customfield_10300"]["allowedValues"][0]["disabled"] = True
    setup(harness, transitions=body)
    check = checks_by_id(harness.run("jira", "requirements", KEY, "--to", "In Progress").data)
    assert check["auto_set:review_flag"]["status"] == "fail"


def test_required_screen_field_is_asked_for(harness: Harness) -> None:
    body = fixture("transitions_in_progress.json")
    body["transitions"][0]["fields"]["resolution"] = {
        "required": True,
        "name": "Resolution",
        "schema": {"type": "resolution", "system": "resolution"},
        "operations": ["set"],
        "allowedValues": [{"id": "10000", "name": "Done"}],
    }
    setup(harness, issue="issue_in_progress.json", transitions=body)
    data = harness.run("jira", "requirements", KEY, "--to", "Done").data
    check = checks_by_id(data)["required:resolution"]
    assert check["status"] == "fail"
    assert "--set 'Resolution=<value>'" in check["fix"]
    assert check["details"]["allowed"] == ["Done"]


def test_explicit_set_on_the_screen_satisfies_a_required_field(harness: Harness) -> None:
    body = fixture("transitions_in_progress.json")
    body["transitions"][0]["fields"]["resolution"] = {
        "required": True,
        "name": "Resolution",
        "schema": {"type": "resolution", "system": "resolution"},
        "operations": ["set"],
        "allowedValues": [{"id": "10000", "name": "Done"}],
    }
    setup(harness, issue="issue_in_progress.json", transitions=body)
    harness.server.add("POST", TRANSITIONS, status=204)
    result = harness.run("jira", "transition", KEY, "--to", "Done", "--set", "resolution=done")
    assert result.code == 0
    (post,) = harness.server.writes()
    assert _body(post)["fields"] == {"resolution": {"id": "10000"}}
    assert result.data["changed"] == [{"field": "Resolution", "id": "resolution"}]


def test_explicit_set_off_the_screen_is_refused(harness: Harness) -> None:
    setup(harness)
    result = harness.run(
        "jira", "transition", KEY, "--to", "In Progress", "--set", "Story Points=3"
    )
    assert result.code == 4
    check = {c["id"]: c for c in result.error["details"]["checks"]}["field:Story Points"]
    assert "not on the 'Start Progress' transition screen" in check["message"]
    assert "yojo jira issue update EXAMPLE-1" in check["fix"]
    assert result.error["details"]["retry_argv"] == [
        "yojo",
        "jira",
        "transition",
        KEY,
        "--to",
        "In Progress",
    ]
    assert harness.server.writes() == []


def test_explicit_set_conflicting_with_auto_set_is_refused(harness: Harness) -> None:
    setup(harness)
    result = harness.run(
        "jira", "transition", KEY, "--to", "In Progress", "--set", "Review Flag=No"
    )
    assert result.code == 4
    check = {c["id"]: c for c in result.error["details"]["checks"]}["auto_set:review_flag"]
    assert "different value" in check["message"]
    assert harness.server.writes() == []


def test_explicit_set_matching_auto_set_is_sent_once(harness: Harness) -> None:
    setup(harness)
    harness.server.add("POST", TRANSITIONS, status=204)
    result = harness.run(
        "jira", "transition", KEY, "--to", "In Progress", "--set", "review flag=yes"
    )
    assert result.code == 0
    (post,) = harness.server.writes()
    assert _body(post)["fields"] == {"customfield_10300": {"id": "1"}}


def test_attachment_requirement_matches_basename(harness: Harness) -> None:
    setup(harness, issue="issue_in_progress.json", transitions="transitions_in_progress.json")
    data = harness.run("jira", "requirements", KEY, "--to", "Done").data
    assert data["ready"] is True
    check = checks_by_id(data)["attachment:{key}_Evidence_*"]
    assert check["details"]["matched"] == ["EXAMPLE-1_Evidence_2026.xml"]


@pytest.mark.parametrize(
    "names",
    [[], ["EXAMPLE-2_Evidence_x.xml"], ["example-1_evidence_x.xml"], ["notes.txt"]],
)
def test_attachment_requirement_missing(harness: Harness, names: list[str]) -> None:
    issue = fixture("issue_in_progress.json")
    issue["fields"]["attachment"] = [{"id": "1", "filename": n} for n in names]
    harness.write_config()
    harness.store()
    harness.server.add("GET", ISSUE, issue)
    harness.server.add("GET", TRANSITIONS, fixture("transitions_in_progress.json"))
    data = harness.run("jira", "requirements", KEY, "--to", "Done").data
    check = checks_by_id(data)["attachment:{key}_Evidence_*"]
    assert check["status"] == "fail"
    assert "EXAMPLE-1_Evidence_*" in check["fix"]
    assert "yojo jira attachment add EXAMPLE-1" in check["fix"]


# --------------------------------------------------------------------------- transition


def _body(request: httpx.Request) -> Any:
    return json.loads(request.content)


def test_transition_success_on_screen(harness: Harness) -> None:
    setup(harness)
    harness.server.add("POST", TRANSITIONS, status=204)
    result = harness.run("jira", "transition", KEY, "--to", "In Progress")
    assert result.code == 0
    assert result.data["atomic"] is True
    assert [o["status"] for o in result.data["operations"]] == ["succeeded"]
    (post,) = harness.server.writes()
    assert _body(post) == {
        "transition": {"id": "11"},
        "fields": {"customfield_10300": {"id": "1"}},
    }


def test_transition_requirements_unmet_writes_nothing(harness: Harness) -> None:
    setup(harness, transitions="transitions_todo_no_screen.json")
    result = harness.run("jira", "transition", KEY, "--to", "In Progress")
    assert result.code == 4
    assert result.error["code"] == "requirements_unmet"
    details = result.error["details"]
    assert details["nothing_sent"] is True
    assert [c["id"] for c in details["checks"]] == ["auto_set:review_flag"]
    assert details["passed"] >= 2
    assert harness.server.writes() == []


def test_transition_pre_edit_put_then_post(harness: Harness) -> None:
    setup(harness, transitions="transitions_todo_no_screen.json", pre_edit="true")
    harness.server.add("PUT", ISSUE, status=204)
    harness.server.add("POST", TRANSITIONS, status=204)
    result = harness.run("jira", "transition", KEY, "--to", "In Progress")
    assert result.code == 0
    assert result.data["atomic"] is False
    assert [(o["op"], o["status"]) for o in result.data["operations"]] == [
        ("edit", "succeeded"),
        ("transition", "succeeded"),
    ]
    put, post = harness.server.writes()
    assert _body(put) == {"fields": {"customfield_10300": {"id": "1"}}}
    assert _body(post) == {"transition": {"id": "11"}}
    sequence = [(r.method, r.url.path) for r in harness.server.requests]
    put_index = sequence.index(("PUT", ISSUE))
    after_put = sequence[put_index + 1 :]
    assert ("GET", ISSUE) in after_put and ("GET", TRANSITIONS) in after_put
    assert after_put[-1] == ("POST", TRANSITIONS)


def test_pre_edit_with_all_fields_on_screen_uses_no_put(harness: Harness) -> None:
    setup(harness, pre_edit="true")
    harness.server.add("POST", TRANSITIONS, status=204)
    result = harness.run("jira", "transition", KEY, "--to", "In Progress")
    assert result.data["atomic"] is True
    assert [r.method for r in harness.server.writes()] == ["POST"]
    assert EDITMETA not in harness.server.paths()


def test_partial_when_post_rejected_after_put(harness: Harness) -> None:
    setup(harness, transitions="transitions_todo_no_screen.json", pre_edit="true")
    harness.server.add("PUT", ISSUE, status=204)
    harness.server.add(
        "POST", TRANSITIONS, {"errorMessages": ["Validator failed"], "errors": {}}, status=400
    )
    result = harness.run("jira", "transition", KEY, "--to", "In Progress")
    assert result.code == 7
    error = result.error
    assert error["code"] == "partial_transition"
    assert error["details"]["atomic"] is False
    assert [(o["op"], o["status"]) for o in error["details"]["operations"]] == [
        ("edit", "succeeded"),
        ("transition", "rejected"),
    ]
    assert error["details"]["remote"]["details"]["messages"] == ["Validator failed"]
    assert "Do not roll back" in error["fix"]
    assert [r.method for r in harness.server.writes()] == ["PUT", "POST"]


def test_partial_when_transition_disappears_after_put(harness: Harness) -> None:
    harness.write_config(config_text(pre_edit="true"))
    harness.store()
    harness.server.add("GET", ISSUE, fixture("issue_todo.json"))
    harness.server.add("GET", TRANSITIONS, fixture("transitions_todo_no_screen.json"))
    harness.server.add("GET", TRANSITIONS, {"transitions": []})
    harness.server.add("GET", EDITMETA, fixture("editmeta.json"))
    harness.server.add("PUT", ISSUE, status=204)
    result = harness.run("jira", "transition", KEY, "--to", "In Progress")
    assert result.code == 7
    ops = result.error["details"]["operations"]
    assert [(o["op"], o["status"]) for o in ops] == [
        ("edit", "succeeded"),
        ("transition", "skipped"),
    ]
    assert "transition_available" in {c["id"] for c in result.error["details"]["checks"]}
    assert "retry" not in result.error["details"]
    assert [r.method for r in harness.server.writes()] == ["PUT"]


def test_partial_when_refresh_fails_after_put(harness: Harness) -> None:
    harness.write_config(config_text(pre_edit="true"))
    harness.store()
    harness.server.add("GET", ISSUE, fixture("issue_todo.json"))
    harness.server.add("GET", ISSUE, {"errorMessages": ["down"]}, status=503)
    harness.server.add("GET", TRANSITIONS, fixture("transitions_todo_no_screen.json"))
    harness.server.add("GET", EDITMETA, fixture("editmeta.json"))
    harness.server.add("PUT", ISSUE, status=204)
    result = harness.run("jira", "transition", KEY, "--to", "In Progress")
    assert result.code == 7
    assert result.error["details"]["refresh_error"]["code"] == "service_error"


def test_unknown_when_post_times_out_after_put(harness: Harness) -> None:
    setup(harness, transitions="transitions_todo_no_screen.json", pre_edit="true")
    harness.server.add("PUT", ISSUE, status=204)

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    harness.server.add("POST", TRANSITIONS, handler=timeout)
    result = harness.run("jira", "transition", KEY, "--to", "In Progress")
    assert result.code == 6
    assert result.error["code"] == "outcome_unknown"
    assert [(o["op"], o["status"]) for o in result.error["details"]["operations"]] == [
        ("edit", "succeeded"),
        ("transition", "unknown"),
    ]
    assert "Inspect" in result.error["fix"]
    assert [r.method for r in harness.server.writes()] == ["PUT", "POST"]


def test_unknown_when_put_times_out(harness: Harness) -> None:
    setup(harness, transitions="transitions_todo_no_screen.json", pre_edit="true")

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    harness.server.add("PUT", ISSUE, handler=timeout)
    result = harness.run("jira", "transition", KEY, "--to", "In Progress")
    assert result.code == 6
    assert [(o["op"], o["status"]) for o in result.error["details"]["operations"]] == [
        ("edit", "unknown"),
        ("transition", "not_attempted"),
    ]
    assert [r.method for r in harness.server.writes()] == ["PUT"]


def _oversized(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b" " * (OVERSIZE_CAP + 1))


OVERSIZE_CAP = 64 * 1024


def test_unknown_when_put_response_is_oversized(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transport, "MAX_RESPONSE_BYTES", OVERSIZE_CAP)
    setup(harness, transitions="transitions_todo_no_screen.json", pre_edit="true")
    harness.server.add("PUT", ISSUE, handler=_oversized)
    result = harness.run("jira", "transition", KEY, "--to", "In Progress")
    assert result.code == 6
    assert result.error["code"] == "outcome_unknown"
    assert [(o["op"], o["status"]) for o in result.error["details"]["operations"]] == [
        ("edit", "unknown"),
        ("transition", "not_attempted"),
    ]
    assert "Inspect" in result.error["fix"]
    assert "Narrow" not in result.error["fix"]
    assert [r.method for r in harness.server.writes()] == ["PUT"]


def test_unknown_when_post_response_is_oversized_after_put(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transport, "MAX_RESPONSE_BYTES", OVERSIZE_CAP)
    setup(harness, transitions="transitions_todo_no_screen.json", pre_edit="true")
    harness.server.add("PUT", ISSUE, status=204)
    harness.server.add("POST", TRANSITIONS, handler=_oversized)
    result = harness.run("jira", "transition", KEY, "--to", "In Progress")
    assert result.code == 6
    assert result.error["code"] == "outcome_unknown"
    assert result.error["details"]["atomic"] is False
    assert [(o["op"], o["status"]) for o in result.error["details"]["operations"]] == [
        ("edit", "succeeded"),
        ("transition", "unknown"),
    ]
    assert "Narrow" not in result.error["fix"]
    assert [r.method for r in harness.server.writes()] == ["PUT", "POST"]


def test_put_rejected_is_remote_validation(harness: Harness) -> None:
    setup(harness, transitions="transitions_todo_no_screen.json", pre_edit="true")
    harness.server.add(
        "PUT", ISSUE, {"errors": {"customfield_10300": "Option invalid"}}, status=400
    )
    result = harness.run("jira", "transition", KEY, "--to", "In Progress")
    assert result.code == 5
    assert result.error["details"]["fields"] == {"customfield_10300": "Option invalid"}
    assert [(o["op"], o["status"]) for o in result.error["details"]["operations"]] == [
        ("edit", "rejected"),
        ("transition", "not_attempted"),
    ]


def test_hidden_validator_rejection(harness: Harness) -> None:
    setup(harness, issue="issue_in_progress.json", transitions="transitions_in_progress.json")
    harness.server.add(
        "POST",
        TRANSITIONS,
        {"errorMessages": ["You must log work first"], "errors": {"worklog": "required"}},
        status=400,
    )
    result = harness.run("jira", "transition", KEY, "--to", "Done")
    assert result.code == 5
    error = result.error
    assert error["code"] == "remote_validation"
    assert "validator" in error["message"]
    assert error["details"]["fields"] == {"worklog": "required"}
    assert [o["status"] for o in error["details"]["operations"]] == ["rejected"]
    assert len(harness.server.writes()) == 1


# --------------------------------------------------------------------------- rules project


def _moved_issue() -> dict[str, Any]:
    moved: dict[str, Any] = fixture("issue_todo.json")
    moved["key"] = "OTHER-5"
    moved["fields"]["project"] = {"key": "OTHER", "id": "10010"}
    return moved


@pytest.mark.parametrize("command", ["requirements", "transition"])
def test_moved_issue_is_refused_before_any_write(harness: Harness, command: str) -> None:
    setup(harness)
    harness.server.routes[("GET", ISSUE)] = []
    harness.server.add("GET", ISSUE, _moved_issue())
    harness.server.add("POST", TRANSITIONS, status=204)
    result = harness.run("jira", command, KEY, "--to", "In Progress")
    assert result.code == 2
    error = result.error
    assert error["code"] == "issue_key_mismatch"
    assert "OTHER-5" in error["fix"]
    assert error["details"] == {
        "requested_key": KEY,
        "current_key": "OTHER-5",
        "current_project": "OTHER",
        "rules_project": "EXAMPLE",
    }
    assert harness.server.writes() == []
    assert TRANSITIONS not in harness.server.paths()


def test_same_key_in_other_project_is_refused(harness: Harness) -> None:
    setup(harness)
    issue = fixture("issue_todo.json")
    issue["fields"]["project"] = {"key": "OTHER", "id": "10010"}
    harness.server.routes[("GET", ISSUE)] = []
    harness.server.add("GET", ISSUE, issue)
    result = harness.run("jira", "transition", KEY, "--to", "In Progress")
    assert result.error["code"] == "issue_key_mismatch"
    assert harness.server.writes() == []


def test_issue_moved_during_pre_edit_skips_transition(harness: Harness) -> None:
    setup(harness, transitions="transitions_todo_no_screen.json", pre_edit="true")
    harness.server.routes[("GET", ISSUE)] = []
    harness.server.add("GET", ISSUE, fixture("issue_todo.json"))
    harness.server.add("GET", ISSUE, _moved_issue())
    harness.server.add("PUT", ISSUE, status=204)
    harness.server.add("POST", TRANSITIONS, status=204)
    result = harness.run("jira", "transition", KEY, "--to", "In Progress")
    assert result.code == 7
    error = result.error
    assert error["code"] == "partial_transition"
    assert error["details"]["refresh_error"]["code"] == "issue_key_mismatch"
    assert [(o["op"], o["status"]) for o in error["details"]["operations"]] == [
        ("edit", "succeeded"),
        ("transition", "skipped"),
    ]
    assert [r.method for r in harness.server.writes()] == ["PUT"]


# --------------------------------------------------------------------------- field collisions


def _two_fields_one_id(pre_edit: str = "false") -> str:
    return (
        config_text(pre_edit=pre_edit)
        .replace(
            "review_flag: {jira_name: Review Flag}",
            "review_flag: {jira_name: Review Flag}\n    started_flag: {id: customfield_10300}",
        )
        .replace("auto_set: {review_flag: Yes}", "auto_set: {review_flag: Yes, started_flag: No}")
    )


@pytest.mark.parametrize(
    ("transitions", "pre_edit"),
    [("transitions_todo.json", "false"), ("transitions_todo_no_screen.json", "true")],
)
def test_auto_set_fields_resolving_to_one_id_are_refused(
    harness: Harness, transitions: str, pre_edit: str
) -> None:
    setup(harness, transitions=transitions, pre_edit=pre_edit)
    harness.write_config(_two_fields_one_id(pre_edit))
    harness.server.add("PUT", ISSUE, status=204)
    harness.server.add("POST", TRANSITIONS, status=204)
    result = harness.run("jira", "transition", KEY, "--to", "In Progress")
    assert result.code == 4
    check = checks_by_id(result.error["details"])["auto_set:started_flag"]
    assert check["status"] == "fail"
    assert "'review_flag' and 'started_flag' both name" in check["message"]
    assert check["details"] == {"field_id": "customfield_10300"}
    assert harness.server.writes() == []


# --------------------------------------------------------------------------- live metadata


@pytest.mark.parametrize(
    "damage",
    [
        "missing_fields",
        "null_fields",
        "list_fields",
        "entry_not_object",
        "no_required",
        "string_required",
        "null_default",
    ],
)
def test_malformed_transition_fields_fail_closed(harness: Harness, damage: str) -> None:
    body = fixture("transitions_todo.json")
    start = body["transitions"][0]
    field = start["fields"]["customfield_10300"]
    if damage == "missing_fields":
        del start["fields"]
    elif damage == "null_fields":
        start["fields"] = None
    elif damage == "list_fields":
        start["fields"] = []
    elif damage == "entry_not_object":
        start["fields"]["customfield_10300"] = None
    elif damage == "no_required":
        del field["required"]
    elif damage == "string_required":
        field["required"] = "true"
    else:
        field["hasDefaultValue"] = None
    setup(harness, transitions=body)
    harness.server.add("POST", TRANSITIONS, status=204)
    for command in ("requirements", "transition"):
        result = harness.run("jira", command, KEY, "--to", "In Progress")
        assert result.code == 6
        assert result.error["code"] == "metadata_incomplete"
    assert harness.server.writes() == []


def test_explicit_empty_transition_fields_is_a_screenless_transition(harness: Harness) -> None:
    setup(harness, issue="issue_in_progress.json", transitions="transitions_in_progress.json")
    assert harness.run("jira", "requirements", KEY, "--to", "Done").data["ready"] is True


def test_malformed_editmeta_field_fails_closed(harness: Harness) -> None:
    setup(harness, transitions="transitions_todo_no_screen.json", pre_edit="true")
    meta = fixture("editmeta.json")
    del meta["fields"]["customfield_10300"]["required"]
    harness.server.routes[("GET", EDITMETA)] = []
    harness.server.add("GET", EDITMETA, meta)
    result = harness.run("jira", "requirements", KEY, "--to", "In Progress")
    assert result.error["code"] == "metadata_incomplete"


# --------------------------------------------------------------------------- refreshed checks


def _refresh_changes(harness: Harness, *, transitions: Any, editmeta: Any) -> Any:
    harness.write_config(config_text(pre_edit="true"))
    harness.store()
    harness.server.add("GET", ISSUE, fixture("issue_todo.json"))
    harness.server.add("GET", TRANSITIONS, fixture("transitions_todo_no_screen.json"))
    harness.server.add("GET", TRANSITIONS, transitions)
    harness.server.add("GET", EDITMETA, fixture("editmeta.json"))
    harness.server.add("GET", EDITMETA, editmeta)
    harness.server.add("PUT", ISSUE, status=204)
    harness.server.add("POST", TRANSITIONS, status=204)
    return harness.run("jira", "transition", KEY, "--to", "In Progress")


def test_partial_when_required_screen_field_appears_after_put(harness: Harness) -> None:
    changed = fixture("transitions_todo_no_screen.json")
    changed["transitions"][0]["fields"] = {
        "customfield_10500": {
            "required": True,
            "hasDefaultValue": False,
            "name": "Sprint Goal",
            "schema": {"type": "string"},
            "operations": ["set"],
        }
    }
    result = _refresh_changes(harness, transitions=changed, editmeta=fixture("editmeta.json"))
    assert result.code == 7
    error = result.error
    assert error["code"] == "partial_transition"
    assert [(o["op"], o["status"]) for o in error["details"]["operations"]] == [
        ("edit", "succeeded"),
        ("transition", "skipped"),
    ]
    refreshed = checks_by_id(error["details"])
    assert refreshed["required:customfield_10500"]["status"] == "fail"
    assert [r.method for r in harness.server.writes()] == ["PUT"]
    assert "ready: true" in error["fix"] and "harmless" not in error["fix"]


def test_partial_when_allowed_value_changes_after_put(harness: Harness) -> None:
    changed = fixture("editmeta.json")
    changed["fields"]["customfield_10300"]["allowedValues"] = [
        {"value": "No", "id": "2", "disabled": False}
    ]
    result = _refresh_changes(
        harness, transitions=fixture("transitions_todo_no_screen.json"), editmeta=changed
    )
    assert result.code == 7
    error = result.error
    assert [(o["op"], o["status"]) for o in error["details"]["operations"]] == [
        ("edit", "succeeded"),
        ("transition", "skipped"),
    ]
    refreshed = checks_by_id(error["details"])
    assert refreshed["auto_set:review_flag"]["status"] == "fail"
    assert [r.method for r in harness.server.writes()] == ["PUT"]
    assert [p for p in harness.server.paths() if p == EDITMETA] == [EDITMETA, EDITMETA]


def _commands(text: str) -> list[list[str]]:
    return [shlex.split(c) for c in re.findall(r"`(yojo [^`]*)`", text)]


def _partial_error(to: str) -> Any:
    return workflow._partial(KEY, to, [], [], [], None, "skipped", "refresh failed", {})


@pytest.mark.parametrize("to", ["Won't Do", 'Say "hi"', "a b; rm -rf ~", "$(x)"])
def test_partial_fix_commands_are_shell_quoted(to: str) -> None:
    error = _partial_error(to)
    assert _commands(error.fix) == [
        ["yojo", "jira", "issue", "get", KEY],
        ["yojo", "jira", "requirements", KEY, "--to", to],
    ]
    assert "Do not roll back" in error.fix
    assert "Inspect" in error.fix and "ready: true" in error.fix
    assert "POSIX shells" in error.fix
    assert "retry" not in error.details and "retry_argv" not in error.details


@pytest.mark.parametrize("to", SHELL_SENTINELS)
def test_partial_fix_commands_stay_literal_in_a_real_posix_shell(to: str, tmp_path: Path) -> None:
    error = _partial_error(to)
    commands = backticked(error.fix, "yojo jira")
    assert [run_in_posix_shell(c, tmp_path) for c in commands] == [
        ["yojo", "jira", "issue", "get", KEY],
        ["yojo", "jira", "requirements", KEY, "--to", to],
    ]
    fix = error.fix
    assert fix.index("Inspect") < fix.index("reconcile") < fix.index("ready: true")
    assert fix.index(commands[0]) < fix.index(commands[1]) < fix.index("ready: true")


@pytest.mark.parametrize("to", SHELL_SENTINELS)
def test_partial_recovery_argv_has_no_transition_retry(to: str) -> None:
    argv = _partial_error(to).details["recovery_argv"]
    assert argv == {
        "inspect": ["yojo", "jira", "issue", "get", KEY],
        "requirements": ["yojo", "jira", "requirements", KEY, "--to", to],
    }
    assert [run_argv(a) for a in argv.values()] == list(argv.values())


def test_recovery_argv_carries_the_sets() -> None:
    argv = workflow.recovery_argv(KEY, "Done", ["Resolution=Won't Do"])
    assert argv["requirements"][-2:] == ["--set", "Resolution=Won't Do"]


@pytest.mark.parametrize("to", SHELL_SENTINELS)
def test_refused_transition_retry_is_shell_quoted(harness: Harness, to: str) -> None:
    setup(harness)
    result = harness.run("jira", "transition", KEY, "--to", to)
    assert result.code == 4
    details = result.error["details"]
    assert details["nothing_sent"] is True
    if details["retry_argv"] is not None:
        assert run_argv(details["retry_argv"]) == details["retry_argv"]
        assert shlex.split(details["retry"]) == details["retry_argv"]
    assert harness.server.writes() == []


def test_refused_transition_retry_applies_the_suggestion(harness: Harness) -> None:
    setup(harness)
    result = harness.run("jira", "transition", KEY, "--to", "In Progres")
    assert result.error["details"]["retry_argv"] == [
        "yojo",
        "jira",
        "transition",
        KEY,
        "--to",
        "In Progress",
    ]
    assert result.error["details"]["retry"] == "yojo jira transition EXAMPLE-1 --to 'In Progress'"


@pytest.mark.parametrize("fields", ["x", [], None, "missing"])
def test_malformed_issue_fields_are_response_invalid(harness: Harness, fields: Any) -> None:
    issue = fixture("issue_todo.json")
    if fields == "missing":
        del issue["fields"]
    else:
        issue["fields"] = fields
    setup(harness)
    harness.server.routes[("GET", ISSUE)] = []
    harness.server.add("GET", ISSUE, issue)
    result = harness.run("jira", "requirements", KEY, "--to", "In Progress")
    assert result.code == 6
    assert result.error["code"] == "response_invalid"
    assert result.error["details"] == {"what": "issue"}
    assert harness.server.writes() == []


# --------------------------------------------------------------------------- field_set rule


def _field_set_config(field: str, project_alias: bool = False) -> str:
    text = config_text().replace(
        '            - kind: attachment_filename\n              pattern: "{key}_Evidence_*"',
        f"            - kind: field_set\n              field: {field}",
    )
    if project_alias:
        text = text.replace(
            "    - key: EXAMPLE\n",
            "    - key: EXAMPLE\n      fields:\n        env: {jira_name: Environment}\n",
        )
    return text


@pytest.mark.parametrize(("field", "alias"), [("environment", False), ("env", True)])
@pytest.mark.parametrize(("value", "ready"), [("Firefox", True), ("  ", False), (None, False)])
def test_field_set_rule_reads_the_current_value(
    harness: Harness, field: str, alias: bool, value: Any, ready: bool
) -> None:
    harness.write_config(_field_set_config(field, alias))
    harness.store()
    issue = fixture("issue_in_progress.json")
    issue["fields"]["customfield_10600"] = value
    harness.server.add("GET", jira_path("/rest/api/2/field"), fixture("fields.json"))
    harness.server.add("GET", ISSUE, issue)
    harness.server.add("GET", TRANSITIONS, fixture("transitions_in_progress.json"))
    data = harness.run("jira", "requirements", KEY, "--to", "Done").data
    assert data["ready"] is ready
    check = checks_by_id(data)[f"field_set:{field}"]
    assert check["status"] == ("pass" if ready else "fail")
    if not ready:
        assert "yojo jira issue update EXAMPLE-1 --set 'Environment=<value>'" in check["fix"]
    requested = harness.server.requests[1].url.params["fields"].split(",")
    assert "customfield_10600" in requested
    assert harness.server.writes() == []
