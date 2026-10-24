from __future__ import annotations

import copy
import json
from typing import Any

import httpx
import pytest

from tests.conftest import Harness, config_text, fixture, jira_path

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
    work_started: str = "Yes",
) -> None:
    harness.write_config(config_text(pre_edit=pre_edit, work_started=work_started))
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
    assert checks["auto_set:work_started"]["details"]["placement"] == "transition"
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
    assert checks["auto_set:work_started"]["status"] == "unverified"
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


def test_requirements_unconfigured_destination_is_offline(harness: Harness) -> None:
    setup(harness)
    result = harness.run("jira", "requirements", KEY, "--to", "Cancelled")
    assert result.code == 2
    assert result.error["code"] == "transition_not_configured"
    assert harness.server.requests == []


def test_requirements_unconfigured_project(harness: Harness) -> None:
    setup(harness)
    result = harness.run("jira", "requirements", "OTHER-1", "--to", "Done")
    assert result.error["code"] == "project_not_configured"


def test_off_screen_auto_set_without_pre_edit_fails(harness: Harness) -> None:
    setup(harness, transitions="transitions_todo_no_screen.json")
    data = harness.run("jira", "requirements", KEY, "--to", "In Progress").data
    check = checks_by_id(data)["auto_set:work_started"]
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
    assert checks_by_id(data)["auto_set:work_started"]["details"]["placement"] == "edit"


def test_pre_edit_not_in_editmeta(harness: Harness) -> None:
    setup(harness, transitions="transitions_todo_no_screen.json", pre_edit="true")
    harness.server.routes[("GET", EDITMETA)] = []
    harness.server.add("GET", EDITMETA, {"fields": {}})
    check = checks_by_id(harness.run("jira", "requirements", KEY, "--to", "In Progress").data)
    assert check["auto_set:work_started"]["status"] == "fail"
    assert "editmeta" in check["auto_set:work_started"]["message"]


def test_pre_edit_without_set_operation(harness: Harness) -> None:
    setup(harness, transitions="transitions_todo_no_screen.json", pre_edit="true")
    meta = fixture("editmeta.json")
    meta["fields"]["customfield_10300"]["operations"] = ["add"]
    harness.server.routes[("GET", EDITMETA)] = []
    harness.server.add("GET", EDITMETA, meta)
    check = checks_by_id(harness.run("jira", "requirements", KEY, "--to", "In Progress").data)
    assert "'set'" in check["auto_set:work_started"]["message"]


def test_disallowed_option_value(harness: Harness) -> None:
    setup(harness, work_started="Maybe")
    check = checks_by_id(harness.run("jira", "requirements", KEY, "--to", "In Progress").data)
    assert check["auto_set:work_started"]["status"] == "fail"
    assert "Yes, No" in check["auto_set:work_started"]["fix"]


def test_disabled_option_is_not_allowed(harness: Harness) -> None:
    body = fixture("transitions_todo.json")
    body["transitions"][0]["fields"]["customfield_10300"]["allowedValues"][0]["disabled"] = True
    setup(harness, transitions=body)
    check = checks_by_id(harness.run("jira", "requirements", KEY, "--to", "In Progress").data)
    assert check["auto_set:work_started"]["status"] == "fail"


def test_required_screen_field_not_configured(harness: Harness) -> None:
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
    check = checks_by_id(data)["screen_field:resolution"]
    assert check["status"] == "fail"
    assert "auto_set" in check["fix"]


def test_attachment_requirement_matches_basename(harness: Harness) -> None:
    setup(harness, issue="issue_in_progress.json", transitions="transitions_in_progress.json")
    data = harness.run("jira", "requirements", KEY, "--to", "Done").data
    assert data["ready"] is True
    check = checks_by_id(data)["attachment:{key}_TestResults_*"]
    assert check["details"]["matched"] == ["EXAMPLE-1_TestResults_2026.xml"]


@pytest.mark.parametrize(
    "names",
    [[], ["EXAMPLE-2_TestResults_x.xml"], ["example-1_testresults_x.xml"], ["notes.txt"]],
)
def test_attachment_requirement_missing(harness: Harness, names: list[str]) -> None:
    issue = fixture("issue_in_progress.json")
    issue["fields"]["attachment"] = [{"id": "1", "filename": n} for n in names]
    harness.write_config()
    harness.store()
    harness.server.add("GET", ISSUE, issue)
    harness.server.add("GET", TRANSITIONS, fixture("transitions_in_progress.json"))
    data = harness.run("jira", "requirements", KEY, "--to", "Done").data
    check = checks_by_id(data)["attachment:{key}_TestResults_*"]
    assert check["status"] == "fail"
    assert "EXAMPLE-1_TestResults_*" in check["fix"]
    assert "atl jira attachment add EXAMPLE-1" in check["fix"]


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
    assert result.error["details"]["checklist"]["ready"] is False
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
    assert result.error["details"]["checklist"]["ready"] is False
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
