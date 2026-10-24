"""Transition requirements: a pure checklist evaluator and the non-atomic write executor."""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

from atlass_cli.config import ProjectConfig, TransitionConfig
from atlass_cli.contracts import AtlError
from atlass_cli.fields import FieldMeta, FieldProblem, resolve, serialize
from atlass_cli.metadata import (
    IssueSnapshot,
    LiveTransition,
    get_editmeta,
    get_issue,
    get_transitions,
)
from atlass_cli.transport import ServiceClient, seg

HIDDEN_VALIDATOR_NOTICE = (
    "This checklist covers configured rules and requirements Jira exposes over REST. Workflow "
    "validators that Jira does not expose can still reject the transition; such a rejection is "
    "reported as remote_validation."
)
NON_ATOMIC_NOTICE = (
    " The plan uses an issue edit (PUT) before the transition (POST). These are separate, "
    "non-atomic writes: the edit persists even if the transition later fails."
)


@dataclass
class Check:
    id: str
    status: str
    message: str
    fix: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "message": self.message,
            "fix": self.fix,
        }
        if self.details:
            out["details"] = self.details
        return out


def checks_pass(checks: list[Check]) -> bool:
    return all(c.status in ("pass", "not_applicable") for c in checks)


def field_collision(check_id: str, first: str, second: str, meta: FieldMeta) -> Check:
    """Two friendly names resolved to one Jira field; sending both would drop a value."""
    return Check(
        check_id,
        "fail",
        f"{first!r} and {second!r} both resolve to {meta.name!r} ({meta.id}); one value would "
        "overwrite the other.",
        f"Map {first!r} and {second!r} to different Jira fields in atl.yaml, or supply only one "
        "of them.",
        {"friendly": [first, second], "field_id": meta.id},
    )


@dataclass(frozen=True)
class PlannedField:
    friendly: str
    id: str
    name: str
    payload: Any

    def as_dict(self) -> dict[str, str]:
        return {"friendly": self.friendly, "id": self.id, "name": self.name}


@dataclass
class Checklist:
    key: str
    to: str
    from_status: str
    transition_name: str | None
    transition: LiveTransition | None
    checks: list[Check]
    edit_fields: list[PlannedField]
    transition_fields: list[PlannedField]

    @property
    def ready(self) -> bool:
        return self.transition is not None and checks_pass(self.checks)

    @property
    def atomic(self) -> bool:
        return not self.edit_fields

    def operations(self, status: str = "planned") -> list[dict[str, Any]]:
        ops: list[dict[str, Any]] = []
        if self.edit_fields:
            ops.append(_operation("edit", "PUT", status, self.edit_fields))
        ops.append(
            _operation(
                "transition",
                "POST",
                status,
                self.transition_fields,
                self.transition.id if self.transition else None,
            )
        )
        return ops

    def as_dict(self) -> dict[str, Any]:
        notice = HIDDEN_VALIDATOR_NOTICE + ("" if self.atomic else NON_ATOMIC_NOTICE)
        return {
            "key": self.key,
            "to": self.to,
            "from_status": self.from_status,
            "transition_name": self.transition_name,
            "ready": self.ready,
            "transition": {"id": self.transition.id, "name": self.transition.name}
            if self.transition
            else None,
            "checks": [c.as_dict() for c in self.checks],
            "plan": {"atomic": self.atomic, "operations": self.operations()},
            "notice": notice,
        }


def _operation(
    op: str,
    method: str,
    status: str,
    fields: list[PlannedField],
    transition_id: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "op": op,
        "method": method,
        "status": status,
        "fields": [f.as_dict() for f in fields],
    }
    if op == "transition":
        out["transition_id"] = transition_id
    if reason is not None:
        out["reason"] = reason
    return out


@dataclass(frozen=True)
class TransitionContext:
    issue: IssueSnapshot
    transitions: list[LiveTransition]
    editmeta: dict[str, FieldMeta] | None


def configured_transitions(project: ProjectConfig, to: str) -> list[TransitionConfig]:
    matches = [t for t in project.transitions.values() if t.to.casefold() == to.casefold()]
    if not matches:
        destinations = sorted({t.to for t in project.transitions.values()})
        raise AtlError(
            "transition_not_configured",
            f"No transition to {to!r} is configured for project {project.key}.",
            f"Use one of the configured destinations: {', '.join(destinations) or '(none)'}; "
            "or add the transition to atl.yaml.",
            {"project": project.key, "to": to, "configured": destinations},
        )
    return matches


def ensure_issue_matches(project: ProjectConfig, key: str, issue: IssueSnapshot) -> None:
    """Refuse to apply ``project``'s policy to an issue Jira reports under another key/project."""
    if issue.key == key and issue.project == project.key:
        return
    raise AtlError(
        "issue_key_mismatch",
        f"Jira returned {issue.key} in project {issue.project} for {key}; atl applies "
        f"{project.key}'s rules only to {project.key} issues (the issue may have moved).",
        f"Re-run with the issue's current key {issue.key}; its project must be configured in "
        "atl.yaml. Nothing was written for this request.",
        {
            "requested_key": key,
            "current_key": issue.key,
            "current_project": issue.project,
            "policy_project": project.key,
        },
    )


def gather(client: ServiceClient, project: ProjectConfig, key: str, to: str) -> TransitionContext:
    issue = get_issue(client, key)
    ensure_issue_matches(project, key, issue)
    candidates = configured_transitions(project, to)
    transitions = get_transitions(client, key)
    current = [
        t for t in candidates if issue.status.casefold() in {s.casefold() for s in t.from_statuses}
    ]
    needs_editmeta = bool(current and requires_editmeta(project, current[0], transitions))
    editmeta = get_editmeta(client, key) if needs_editmeta else None
    return TransitionContext(issue=issue, transitions=transitions, editmeta=editmeta)


def requires_editmeta(
    project: ProjectConfig, config: TransitionConfig, transitions: list[LiveTransition]
) -> bool:
    """Only off-screen values in an opted-in transition need edit metadata."""
    if not config.pre_edit or not config.auto_set:
        return False
    live = [t for t in transitions if t.to.casefold() == config.to.casefold()]
    if len(live) != 1:
        return False
    for friendly in config.auto_set:
        try:
            if resolve(project.fields[friendly], live[0].fields) is None:
                return True
        except FieldProblem:
            # The checklist will report the ambiguous screen mapping.
            pass
    return False


def evaluate(
    project: ProjectConfig,
    to: str,
    ctx: TransitionContext,
    *,
    check_source: bool = True,
    check_attachments: bool = True,
) -> Checklist:
    """Pure evaluation of every configured and live requirement for one transition."""
    issue = ctx.issue
    candidates = configured_transitions(project, to)
    checks: list[Check] = []

    status_cf = issue.status.casefold()
    matching = [t for t in candidates if status_cf in {s.casefold() for s in t.from_statuses}]
    config = matching[0] if matching else candidates[0]
    if check_source:
        if matching:
            checks.append(Check("source_status", "pass", f"{issue.key} is in {issue.status!r}."))
        else:
            sources = sorted({s for t in candidates for s in t.from_statuses})
            checks.append(
                Check(
                    "source_status",
                    "fail",
                    f"{issue.key} is in {issue.status!r}; moving to {config.to!r} requires one "
                    f"of: {', '.join(sources)}.",
                    f"Move {issue.key} to {' or '.join(repr(s) for s in sources)} first, then "
                    "check requirements again.",
                    {"current": issue.status, "allowed_from": sources},
                )
            )

    live = [t for t in ctx.transitions if t.to.casefold() == config.to.casefold()]
    transition: LiveTransition | None = None
    if len(live) == 1:
        transition = live[0]
        checks.append(
            Check(
                "transition_available",
                "pass",
                f"Jira offers transition {transition.name!r} (id {transition.id}) to "
                f"{transition.to!r}.",
            )
        )
    elif not live:
        offered = sorted({t.to for t in ctx.transitions})
        checks.append(
            Check(
                "transition_available",
                "fail",
                f"Jira does not currently offer a transition from {issue.status!r} to "
                f"{config.to!r} for {issue.key}.",
                "Check the issue's workflow status, workflow conditions, and your permissions; "
                f"currently offered destinations: {', '.join(offered) or '(none)'}.",
                {"offered": offered},
            )
        )
    else:
        checks.append(
            Check(
                "transition_available",
                "fail",
                f"Jira offers {len(live)} transitions to {config.to!r}; atl will not guess.",
                "Ask a Jira admin to leave a single transition to this status from "
                f"{issue.status!r}.",
                {"transition_ids": sorted(t.id for t in live)},
            )
        )

    edit_fields: list[PlannedField] = []
    transition_fields: list[PlannedField] = []
    covered: dict[str, str] = {}
    screen = transition.fields if transition else {}

    for friendly, value in config.auto_set.items():
        ref = project.fields[friendly]
        check_id = f"auto_set:{friendly}"
        if transition is None:
            checks.append(
                Check(
                    check_id,
                    "unverified",
                    f"Placement of {ref.label!r} cannot be checked until the transition is "
                    "available.",
                    "Resolve the transition_available check first.",
                )
            )
            continue
        try:
            on_screen = resolve(ref, screen)
            if on_screen is not None:
                if on_screen.id in covered:
                    checks.append(
                        field_collision(check_id, covered[on_screen.id], friendly, on_screen)
                    )
                    continue
                payload = serialize(ref, on_screen, value, require_set=False)
                transition_fields.append(
                    PlannedField(friendly, on_screen.id, on_screen.name, payload)
                )
                covered[on_screen.id] = friendly
                checks.append(
                    Check(
                        check_id,
                        "pass",
                        f"{on_screen.name!r} will be set to {value!r} in the transition request.",
                        details={"placement": "transition", "field_id": on_screen.id},
                    )
                )
                continue
            if not config.pre_edit:
                checks.append(
                    Check(
                        check_id,
                        "fail",
                        f"{ref.label!r} is not on the {transition.name!r} transition screen and "
                        f"pre_edit is off for transition {config.name!r}.",
                        f"Ask a Jira admin to add {ref.label!r} to the transition screen, or set "
                        f"pre_edit: true under transitions.{config.name} in atl.yaml to edit it "
                        "in a separate, non-atomic request first.",
                        {"placement": "none"},
                    )
                )
                continue
            editable = resolve(ref, ctx.editmeta or {})
            if editable is None:
                checks.append(
                    Check(
                        check_id,
                        "fail",
                        f"{ref.label!r} is neither on the transition screen nor editable on "
                        f"{issue.key} (not in editmeta).",
                        f"Ask a Jira admin to add {ref.label!r} to the edit screen for this issue "
                        "type, or remove it from auto_set.",
                        {"placement": "none"},
                    )
                )
                continue
            if editable.id in covered:
                checks.append(field_collision(check_id, covered[editable.id], friendly, editable))
                continue
            payload = serialize(ref, editable, value, require_set=True)
            edit_fields.append(PlannedField(friendly, editable.id, editable.name, payload))
            covered[editable.id] = friendly
            checks.append(
                Check(
                    check_id,
                    "pass",
                    f"{editable.name!r} is off-screen and will be set to {value!r} in a separate "
                    "edit (PUT) before the transition.",
                    details={"placement": "edit", "field_id": editable.id},
                )
            )
        except FieldProblem as problem:
            checks.append(Check(check_id, "fail", problem.message, problem.fix, problem.details))

    for field_id, meta in sorted(screen.items()):
        if not meta.required or meta.has_default or field_id in covered:
            continue
        checks.append(
            Check(
                f"screen_field:{field_id}",
                "fail",
                f"Transition {transition.name if transition else ''!r} requires {meta.name!r} "
                f"({field_id}), which atl.yaml does not set.",
                f"Map {meta.name!r} under fields and add it to transitions.{config.name}.auto_set "
                "in atl.yaml with an allowed value.",
                {"field_id": field_id},
            )
        )

    for requirement in config.require:
        expanded = requirement.expand(issue.key)
        check_id = f"attachment:{requirement.pattern}"
        if not check_attachments:
            checks.append(
                Check(check_id, "not_applicable", "Attachment rules are checked per issue.")
            )
            continue
        matched = sorted(name for name in issue.attachments if fnmatchcase(name, expanded))
        if matched:
            checks.append(
                Check(
                    check_id,
                    "pass",
                    f"Found attachment(s) matching {expanded!r}.",
                    details={"matched": matched},
                )
            )
        else:
            checks.append(
                Check(
                    check_id,
                    "fail",
                    f"No attachment on {issue.key} matches {expanded!r}.",
                    f"Attach a file whose name matches {expanded!r} with `atl jira attachment add "
                    f"{issue.key} --file PATH`, then retry.",
                    {"pattern": expanded, "attachments": sorted(issue.attachments)[:50]},
                )
            )

    return Checklist(
        key=issue.key,
        to=config.to,
        from_status=issue.status,
        transition_name=config.name,
        transition=transition,
        checks=checks,
        edit_fields=edit_fields,
        transition_fields=transition_fields,
    )


def requirements(client: ServiceClient, project: ProjectConfig, key: str, to: str) -> Checklist:
    return evaluate(project, to, gather(client, project, key, to))


def _ops(
    edit: tuple[str, list[PlannedField]] | None,
    transition: tuple[str, list[PlannedField], str | None],
    reason: str | None = None,
) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = []
    if edit is not None:
        ops.append(_operation("edit", "PUT", edit[0], edit[1]))
    ops.append(
        _operation("transition", "POST", transition[0], transition[1], transition[2], reason)
    )
    return ops


def _failed_status(exc: AtlError) -> str:
    return "rejected" if exc.write_outcome == "rejected" else "not_attempted"


def _retry_hint(key: str, to: str) -> str:
    return f"`atl jira requirements {key} --to '{to}'`"


def transition(client: ServiceClient, project: ProjectConfig, key: str, to: str) -> dict[str, Any]:
    checklist = requirements(client, project, key, to)
    if not checklist.ready:
        raise AtlError(
            "requirements_unmet",
            f"{key} does not meet the requirements to move to {checklist.to!r}; nothing was "
            "written.",
            "Resolve each failed check using its fix, then retry.",
            {"checklist": checklist.as_dict()},
        )
    live = checklist.transition
    assert live is not None
    edit_fields = checklist.edit_fields
    edit_done = False

    if edit_fields:
        try:
            client.request(
                "PUT",
                f"/rest/api/2/issue/{seg(key)}",
                json_body={"fields": {f.id: f.payload for f in edit_fields}},
            )
        except AtlError as exc:
            if exc.write_outcome == "unknown":
                exc.details.update(
                    key=key,
                    atomic=False,
                    operations=_ops(
                        ("unknown", edit_fields),
                        ("not_attempted", checklist.transition_fields, live.id),
                    ),
                )
                exc.fix = (
                    f"Inspect {key} to see whether the edit was applied ({_retry_hint(key, to)}) "
                    "before running the transition again. atl did not retry."
                )
                raise
            exc.details.update(
                key=key,
                atomic=False,
                operations=_ops(
                    (_failed_status(exc), edit_fields),
                    ("not_attempted", checklist.transition_fields, live.id),
                ),
            )
            raise
        edit_done = True
        try:
            refreshed = requirements(client, project, key, to)
        except AtlError as exc:
            raise _partial(
                key,
                to,
                edit_fields,
                checklist.transition_fields,
                live.id,
                "skipped",
                f"refreshing issue state after the edit failed ({exc.code})",
                {"refresh_error": exc.envelope()["error"]},
            ) from None
        if not refreshed.ready:
            raise _partial(
                key,
                to,
                edit_fields,
                refreshed.transition_fields,
                refreshed.transition.id if refreshed.transition else None,
                "skipped",
                "requirements no longer pass after the edit",
                {"checklist": refreshed.as_dict()},
            )
        checklist = refreshed
        assert refreshed.transition is not None
        live = refreshed.transition

    body: dict[str, Any] = {"transition": {"id": live.id}}
    if checklist.transition_fields:
        body["fields"] = {f.id: f.payload for f in checklist.transition_fields}
    edit_state = ("succeeded", edit_fields) if edit_done else None
    try:
        client.request("POST", f"/rest/api/2/issue/{seg(key)}/transitions", json_body=body)
    except AtlError as exc:
        if exc.write_outcome == "unknown":
            exc.details.update(
                key=key,
                atomic=not edit_done,
                operations=_ops(
                    edit_state,
                    ("unknown", checklist.transition_fields, live.id),
                ),
            )
            exc.fix = (
                f"Inspect {key}'s current status ({_retry_hint(key, to)}) before retrying; the "
                "transition may or may not have been applied. atl did not retry."
            )
            raise
        if edit_done:
            raise _partial(
                key,
                to,
                edit_fields,
                checklist.transition_fields,
                live.id,
                _failed_status(exc),
                f"the transition request failed ({exc.code})",
                {"remote": exc.envelope()["error"]},
            ) from None
        exc.details.update(
            key=key,
            atomic=True,
            operations=_ops(None, (_failed_status(exc), checklist.transition_fields, live.id)),
            notice=HIDDEN_VALIDATOR_NOTICE,
        )
        if exc.code == "remote_validation":
            exc.message = (
                f"Jira rejected the transition of {key} after preflight passed (likely a "
                "workflow validator not exposed over REST)."
            )
        raise

    return {
        "key": key,
        "from_status": checklist.from_status,
        "to": checklist.to,
        "transition_id": live.id,
        "atomic": not edit_done,
        "operations": _ops(edit_state, ("succeeded", checklist.transition_fields, live.id)),
    }


def _partial(
    key: str,
    to: str,
    edit_fields: list[PlannedField],
    transition_fields: list[PlannedField],
    transition_id: str | None,
    transition_status: str,
    reason: str,
    extra: dict[str, Any],
) -> AtlError:
    return AtlError(
        "partial_transition",
        f"The edit of {key} was applied but the transition to {to!r} was not: {reason}.",
        f"Do not roll back. Inspect {key}'s current status and field values (the edit may "
        "have overwritten a concurrent change), reconcile them with the issue owner, resolve "
        f"the failed checks or remote errors, and confirm a fresh {_retry_hint(key, to)} is "
        f"ready before running `atl jira transition {key} --to '{to}'` again. A retry repeats "
        "the edit (PUT) unconditionally.",
        {
            "key": key,
            "atomic": False,
            "operations": _ops(
                ("succeeded", edit_fields),
                (transition_status, transition_fields, transition_id),
                reason,
            ),
            **extra,
        },
    )
