"""Checks before writes: the shared check type, field planning, and transitions.

Every write collects all of its independent checks first and refuses with every problem when
any fails or cannot be verified, so nothing is sent. A transition checklist combines live Jira
transitions and screens with optional ``yojo.yaml`` rules; the executor keeps the non-atomic
pre-edit sequence honest with ``partial_transition`` and ``outcome_unknown``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

from yojo.config import FieldRef, JiraConfig, ProjectConfig, TransitionConfig
from yojo.contracts import YojoError
from yojo.fields import (
    FieldMeta,
    FieldProblem,
    allowed_labels,
    coerce,
    resolve,
    unresolved_problem,
)
from yojo.metadata import (
    IssueSnapshot,
    LiveTransition,
    all_fields,
    get_editmeta,
    get_issue,
    get_transitions,
)
from yojo.resolve import Candidate, Unresolved, UserLookup, pick
from yojo.retry import POSIX_NOTE, PROGRAM, Edit, placeholder, posix
from yojo.transport import ServiceClient, sanitize_text, seg

HIDDEN_VALIDATOR_NOTICE = (
    "This checklist covers yojo.yaml rules and the requirements Jira exposes over REST. Workflow "
    "validators that Jira does not expose can still reject the transition; such a rejection is "
    "reported as remote_validation."
)
NON_ATOMIC_NOTICE = (
    " The plan uses an issue edit (PUT) before the transition (POST). These are separate, "
    "non-atomic writes: the edit persists even if the transition later fails."
)
MAX_LISTED = 10


@dataclass
class Check:
    id: str
    status: str
    message: str
    fix: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    retry: Edit | None = field(default=None, compare=False, repr=False)

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


def failed(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.status not in ("pass", "not_applicable")]


def refusal(what: str, checks: list[Check], extra: dict[str, Any] | None = None) -> YojoError:
    """One response for every failed or unverified check; nothing was sent."""
    bad = failed(checks)
    error = YojoError(
        "requirements_unmet",
        f"{what} was refused with {len(bad)} problem(s); nothing was sent.",
        "Apply each check's fix, then run details.retry (placeholders in <angle brackets> need "
        "your content).",
        {
            "nothing_sent": True,
            "checks": [c.as_dict() for c in bad],
            "passed": len(checks) - len(bad),
            **(extra or {}),
        },
    )
    error.checks = checks
    return error


@dataclass(frozen=True)
class Assignment:
    """A requested field value: ``name`` as the caller wrote it; ``source`` is flag/input/config."""

    name: str
    value: Any
    source: str = "flag"


@dataclass(frozen=True)
class PlannedField:
    given: str
    id: str
    name: str
    payload: Any

    def as_dict(self) -> dict[str, str]:
        return {"field": self.name, "id": self.id}


def _value_edit(given: str, source: str, replacement: str | None) -> Edit | None:
    if source == "config":
        return None
    return Edit("set", given, replacement or placeholder(), source)


def plan_field(
    assignment: Assignment,
    pool: dict[str, FieldMeta],
    aliases: dict[str, FieldRef],
    users: UserLookup,
    covered: dict[str, PlannedField],
    *,
    scope: str,
    reserved: dict[str, str] | None = None,
) -> tuple[PlannedField | None, Check]:
    """Resolve and coerce one assignment against ``pool``; return the plan and its check."""
    given = assignment.name
    check_id = f"field:{given}"
    try:
        meta = resolve(given, pool, aliases, scope)
    except Unresolved as exc:
        problem = unresolved_problem(exc)
        edit = None
        if assignment.source != "config":
            if exc.kind == "ambiguous":
                choice = "|".join(c.pick_as for c in exc.candidates[:MAX_LISTED])
                edit = Edit("rename", given, placeholder(choice), assignment.source)
            elif exc.replacement is not None:
                edit = Edit("rename", given, exc.replacement, assignment.source)
            else:
                edit = Edit("drop", given, None, assignment.source)
        return None, Check(check_id, "fail", problem.message, problem.fix, problem.details, edit)
    if reserved and meta.id in reserved:
        return None, Check(
            check_id,
            "fail",
            f"{meta.name} is set by {reserved[meta.id]}, not --set.",
            f"Pass {meta.name} with {reserved[meta.id]} and remove the --set for {given!r}.",
            {"field_id": meta.id},
            Edit("drop", given, None, assignment.source) if assignment.source != "config" else None,
        )
    if meta.id in covered:
        first = covered[meta.id]
        return None, Check(
            check_id,
            "fail",
            f"{first.given!r} and {given!r} both name {meta.shown}; one value would overwrite "
            "the other.",
            f"Give {meta.name} only once.",
            {"field_id": meta.id},
            Edit("drop", given, None, assignment.source) if assignment.source != "config" else None,
        )
    try:
        payload = coerce(meta, assignment.value, users)
    except FieldProblem as problem:
        edit = None
        if problem.status == "fail":
            edit = _value_edit(given, assignment.source, problem.replacement)
        details = {"field_id": meta.id, **problem.details}
        return None, Check(check_id, problem.status, problem.message, problem.fix, details, edit)
    planned = PlannedField(given, meta.id, meta.name, payload)
    covered[meta.id] = planned
    return planned, Check(
        check_id, "pass", f"{meta.shown} is valid.", details={"field_id": meta.id}
    )


def attempted(checks: list[Check]) -> set[str]:
    """Field IDs the caller supplied with a value that failed; already reported, not missing."""
    return {
        c.details["field_id"]
        for c in failed(checks)
        if c.id.startswith("field:") and isinstance(c.details.get("field_id"), str)
    }


def missing_required(meta: FieldMeta, what: str) -> Check:
    """A required field nobody supplied: ask for it, listing allowed values when known."""
    allowed = allowed_labels(meta)[:MAX_LISTED] if meta.allowed_values is not None else []
    hint = f" Allowed values: {', '.join(repr(a) for a in allowed)}." if allowed else ""
    details: dict[str, Any] = {"field_id": meta.id}
    if allowed:
        details["allowed"] = allowed
    return Check(
        f"required:{meta.id}",
        "fail",
        f"Jira requires {meta.shown} {what}.",
        f"Add --set '{meta.name}=<value>'.{hint}",
        details,
        Edit("set", meta.name, placeholder(), "missing"),
    )


# --------------------------------------------------------------------------- transitions


@dataclass
class Checklist:
    key: str
    to: str
    from_status: str
    rule: str | None
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
            "rule": self.rule,
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
    field_index: dict[str, FieldMeta] | None = None


def project_of(key: str) -> str:
    return key.rpartition("-")[0]


def rules_for(project: ProjectConfig | None, to: str) -> list[TransitionConfig]:
    if project is None:
        return []
    return [t for t in project.transitions.values() if t.to.casefold() == to.casefold()]


def _current_rule(rules: list[TransitionConfig], status: str) -> TransitionConfig | None:
    matching = [t for t in rules if status.casefold() in {s.casefold() for s in t.from_statuses}]
    if matching:
        return matching[0]
    return rules[0] if rules else None


def ensure_issue_matches(project: ProjectConfig, key: str, issue: IssueSnapshot) -> None:
    """Refuse to apply ``project``'s rules to an issue Jira reports under another key/project."""
    if issue.key == key and issue.project == project.key:
        return
    raise YojoError(
        "issue_key_mismatch",
        f"Jira returned {issue.key} in project {issue.project} for {key}; yojo applies "
        f"{project.key}'s rules only to {project.key} issues (the issue may have moved). Nothing "
        "was sent.",
        "Re-run with the issue's current key: "
        f"{posix([PROGRAM, 'jira', 'issue', 'get', issue.key])}.",
        {
            "requested_key": key,
            "current_key": issue.key,
            "current_project": issue.project,
            "rules_project": project.key,
        },
    )


def _match_transition(
    transitions: list[LiveTransition], to: str, issue: IssueSnapshot
) -> tuple[LiveTransition | None, Check]:
    """Find the one live transition for ``to``, by destination status or transition name."""
    folded = to.strip().casefold()
    live = [t for t in transitions if t.to.casefold() == folded]
    if not live:
        live = [t for t in transitions if t.name.casefold() == folded]
    if len(live) == 1:
        chosen = live[0]
        return chosen, Check(
            "transition_available",
            "pass",
            f"Jira offers {chosen.name!r} to {chosen.to!r} for {issue.key}.",
            details={"transition_id": chosen.id},
        )
    if live:
        names = sorted(t.name for t in live)
        return None, Check(
            "transition_available",
            "fail",
            f"Jira offers {len(live)} transitions to {to!r} for {issue.key}: "
            f"{', '.join(repr(n) for n in names)}; yojo will not guess.",
            f"Pass the transition name with --to, one of: {', '.join(repr(n) for n in names)}.",
            {"transitions": names, "transition_ids": sorted(t.id for t in live)},
            Edit("flag", "--to", placeholder("|".join(names))),
        )
    if issue.status.casefold() == folded:
        return None, Check(
            "transition_available",
            "fail",
            f"{issue.key} is already in {issue.status!r}.",
            f"Nothing to do; check the status with "
            f"{posix([PROGRAM, 'jira', 'issue', 'get', issue.key])}.",
            {"current": issue.status},
        )
    offered = sorted({t.to for t in transitions})
    pool = [Candidate(label=t.to, value=t, names=(t.name,)) for t in transitions]
    try:
        pick(to, pool, "status")
    except Unresolved as exc:
        suggestion = exc.replacement
    else:  # pragma: no cover - a direct hit is handled above
        suggestion = None
    message = f"Jira does not offer a move from {issue.status!r} to {to!r} for {issue.key}."
    if suggestion is not None:
        message += f" Did you mean {suggestion!r}?"
    return None, Check(
        "transition_available",
        "fail",
        message,
        (f"Use --to {suggestion!r}." if suggestion else "")
        + (f" Statuses offered now: {', '.join(repr(o) for o in offered)}." if offered else "")
        + (
            ""
            if suggestion or offered
            else " Jira offers no transitions for this issue to you now."
        ),
        {"offered": offered, **({"suggestion": suggestion} if suggestion else {})},
        Edit("flag", "--to", suggestion) if suggestion else None,
    )


def _field_set_ids(
    jira: JiraConfig,
    project: str,
    rules: list[TransitionConfig],
    index: dict[str, FieldMeta] | None,
) -> list[str]:
    if index is None:
        return []
    aliases = jira.aliases(project)
    ids: list[str] = []
    for rule in rules:
        for requirement in rule.require:
            if requirement.kind != "field_set" or requirement.field is None:
                continue
            try:
                ids.append(resolve(requirement.field, index, aliases).id)
            except Unresolved:
                continue
    return ids


def gather(client: ServiceClient, jira: JiraConfig, key: str, to: str) -> TransitionContext:
    project = jira.project(project_of(key))
    rules = rules_for(project, to)
    needs_index = any(r.kind == "field_set" for rule in rules for r in rule.require)
    index = all_fields(client) if needs_index else None
    issue = get_issue(client, key, _field_set_ids(jira, project_of(key), rules, index))
    if project is not None:
        ensure_issue_matches(project, key, issue)
    transitions = get_transitions(client, key)
    rule = _current_rule(rules, issue.status)
    editmeta = (
        get_editmeta(client, key)
        if rule is not None and requires_editmeta(jira, project_of(key), rule, transitions)
        else None
    )
    return TransitionContext(
        issue=issue, transitions=transitions, editmeta=editmeta, field_index=index
    )


def requires_editmeta(
    jira: JiraConfig, project: str, rule: TransitionConfig, transitions: list[LiveTransition]
) -> bool:
    """Only off-screen values in an opted-in rule need edit metadata."""
    if not rule.pre_edit or not rule.auto_set:
        return False
    live = [t for t in transitions if t.to.casefold() == rule.to.casefold()]
    if len(live) != 1:
        return False
    aliases = jira.aliases(project)
    for name in rule.auto_set:
        try:
            resolve(name, live[0].fields, aliases)
        except Unresolved as exc:
            if exc.kind == "not_found":
                return True
    return False


def evaluate(
    jira: JiraConfig,
    to: str,
    ctx: TransitionContext,
    requested: list[Assignment],
    users: UserLookup,
) -> Checklist:
    """Every configured and live requirement for one transition, with no network access."""
    issue = ctx.issue
    project = jira.project(issue.project)
    aliases = jira.aliases(issue.project)
    checks: list[Check] = []

    transition, match_check = _match_transition(ctx.transitions, to, issue)
    destination = transition.to if transition else to
    rules = rules_for(project, destination)
    rule = _current_rule(rules, issue.status)
    if rules and rule is not None:
        if issue.status.casefold() in {s.casefold() for s in rule.from_statuses}:
            checks.append(Check("source_status", "pass", f"{issue.key} is in {issue.status!r}."))
        else:
            sources = sorted({s for t in rules for s in t.from_statuses})
            checks.append(
                Check(
                    "source_status",
                    "fail",
                    f"{issue.key} is in {issue.status!r}; yojo.yaml allows moving to "
                    f"{rule.to!r} only from {', '.join(repr(s) for s in sources)}.",
                    f"Move {issue.key} to {' or '.join(repr(s) for s in sources)} first.",
                    {"current": issue.status, "allowed_from": sources},
                )
            )
    checks.append(match_check)

    covered: dict[str, PlannedField] = {}
    edit_fields: list[PlannedField] = []
    transition_fields: list[PlannedField] = []
    screen = transition.fields if transition else {}
    scope = f"on the {transition.name!r} screen" if transition else ""

    for assignment in requested:
        check_id = f"field:{assignment.name}"
        if transition is None:
            checks.append(
                Check(
                    check_id,
                    "unverified",
                    f"{assignment.name!r} cannot be checked until the transition is available.",
                    "Resolve the transition_available check first.",
                )
            )
            continue
        try:
            resolve(assignment.name, screen, aliases)
        except Unresolved as exc:
            if exc.kind == "not_found" and exc.replacement is None:
                example = posix(
                    [
                        PROGRAM,
                        "jira",
                        "issue",
                        "update",
                        issue.key,
                        "--set",
                        f"{assignment.name}=<value>",
                    ]
                )
                checks.append(
                    Check(
                        check_id,
                        "fail",
                        f"{assignment.name!r} is not on the {transition.name!r} transition screen.",
                        f"Set it before the transition with {example}, and remove it here.",
                        {"screen": sorted(m.name for m in screen.values())[:MAX_LISTED]},
                        Edit("drop", assignment.name, None, assignment.source),
                    )
                )
                continue
        planned, check = plan_field(assignment, screen, aliases, users, covered, scope=scope)
        if planned is not None:
            transition_fields.append(planned)
            check.message = f"{planned.name} will be set in the transition."
            check.details["placement"] = "transition"
        checks.append(check)

    explicit_ids = set(covered)
    for name, value in (rule.auto_set if rule else {}).items():
        assignment = Assignment(name, value, "config")
        check_id = f"auto_set:{name}"
        if transition is None:
            checks.append(
                Check(
                    check_id,
                    "unverified",
                    f"Placement of {name!r} cannot be checked until the transition is available.",
                    "Resolve the transition_available check first.",
                )
            )
            continue
        assert rule is not None
        on_screen = True
        try:
            meta = resolve(name, screen, aliases)
        except Unresolved as exc:
            if exc.kind != "not_found":
                problem = unresolved_problem(exc)
                checks.append(
                    Check(check_id, "fail", problem.message, problem.fix, problem.details)
                )
                continue
            on_screen = False
        if on_screen and meta.id in explicit_ids:
            requested_value = covered[meta.id]
            try:
                configured = coerce(meta, value, users)
            except FieldProblem as problem:
                checks.append(Check(check_id, problem.status, problem.message, problem.fix))
                continue
            if configured != requested_value.payload:
                checks.append(
                    Check(
                        check_id,
                        "fail",
                        f"yojo.yaml sets {meta.name} to {value!r} for this transition, but "
                        f"{requested_value.given!r} asks for a different value.",
                        f"Remove the --set for {requested_value.given!r}; the team rule sets it.",
                        {"field_id": meta.id},
                        Edit("drop", requested_value.given, None, "flag"),
                    )
                )
            else:
                checks.append(Check(check_id, "pass", f"{meta.name} matches yojo.yaml."))
            continue
        if on_screen:
            planned, check = plan_field(assignment, screen, aliases, users, covered, scope=scope)
            if planned is not None:
                transition_fields.append(planned)
                check.message = f"{planned.name} will be set to {value!r} in the transition."
                check.details["placement"] = "transition"
            check.id = check_id
            checks.append(check)
            continue
        if not rule.pre_edit:
            checks.append(
                Check(
                    check_id,
                    "fail",
                    f"{name!r} is not on the {transition.name!r} transition screen and pre_edit "
                    f"is off for rule {rule.name!r}.",
                    f"Ask a Jira admin to add {name!r} to the transition screen, or set "
                    f"pre_edit: true under transitions.{rule.name} in yojo.yaml to edit it in a "
                    "separate, non-atomic request first.",
                    {"placement": "none"},
                )
            )
            continue
        if ctx.editmeta is None:
            checks.append(
                Check(
                    check_id,
                    "unverified",
                    f"yojo could not read the edit screen for {issue.key}, so {name!r} cannot "
                    "be checked.",
                    "Run the command again; if it repeats, ask a Jira admin to check editmeta.",
                )
            )
            continue
        planned, check = plan_field(
            assignment, ctx.editmeta, aliases, users, covered, scope="on the edit screen"
        )
        check.id = check_id
        if planned is not None:
            edit_fields.append(planned)
            check.message = (
                f"{planned.name} is off the transition screen and will be set to {value!r} in a "
                "separate edit (PUT) before the transition."
            )
            check.details["placement"] = "edit"
        elif check.message.startswith("No field"):
            check.message = (
                f"{name!r} is neither on the transition screen nor editable on {issue.key}."
            )
            check.fix = (
                f"Ask a Jira admin to add {name!r} to the edit screen for this issue type, or "
                f"remove it from transitions.{rule.name}.auto_set in yojo.yaml."
            )
        checks.append(check)

    given_ids = attempted(checks)
    for field_id, meta in sorted(screen.items()):
        if not meta.required or meta.has_default or field_id in covered or field_id in given_ids:
            continue
        checks.append(missing_required(meta, f"to move {issue.key} to {destination!r}"))

    planned_ids = {f.id for f in (*edit_fields, *transition_fields)}
    for requirement in rule.require if rule else ():
        if requirement.kind == "attachment_filename":
            checks.append(_attachment_check(issue, requirement.expand(issue.key), requirement))
        else:
            checks.append(
                _field_set_check(issue, requirement.field or "", ctx, aliases, planned_ids, screen)
            )

    return Checklist(
        key=issue.key,
        to=destination,
        from_status=issue.status,
        rule=rule.name if rule else None,
        transition=transition,
        checks=checks,
        edit_fields=edit_fields,
        transition_fields=transition_fields,
    )


def _attachment_check(issue: IssueSnapshot, expanded: str, requirement: Any) -> Check:
    check_id = f"attachment:{requirement.pattern}"
    matched = sorted(name for name in issue.attachments if fnmatchcase(name, expanded))
    if matched:
        return Check(
            check_id,
            "pass",
            f"Found attachment(s) matching {expanded!r}.",
            details={"matched": matched[:MAX_LISTED]},
        )
    return Check(
        check_id,
        "fail",
        f"No attachment on {issue.key} matches {expanded!r}.",
        f"Attach a file whose name matches {expanded!r} with "
        f"{posix([PROGRAM, 'jira', 'attachment', 'add', issue.key, '--file', '<file>'])}.",
        {"pattern": expanded, "attachments": sorted(issue.attachments)[:MAX_LISTED]},
    )


def _filled(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _field_set_check(
    issue: IssueSnapshot,
    name: str,
    ctx: TransitionContext,
    aliases: dict[str, FieldRef],
    planned_ids: set[str],
    screen: dict[str, FieldMeta],
) -> Check:
    check_id = f"field_set:{name}"
    if ctx.field_index is None:
        return Check(
            check_id,
            "unverified",
            f"yojo could not read Jira's field list, so {name!r} cannot be checked.",
            "Run the command again; if it repeats, check access to Jira's field list.",
        )
    try:
        meta = resolve(name, ctx.field_index, aliases)
    except Unresolved as exc:
        return Check(
            check_id,
            "fail",
            f"yojo.yaml requires {name!r} to be set, but {exc.message}",
            f"Fix the field name in the field_set rule in yojo.yaml. {exc.fix}",
            exc.details(),
        )
    if meta.id in planned_ids:
        return Check(check_id, "pass", f"{meta.name} is set by this transition.")
    if _filled(issue.values.get(meta.id)):
        return Check(check_id, "pass", f"{meta.name} has a value on {issue.key}.")
    update = posix([PROGRAM, "jira", "issue", "update", issue.key, "--set", f"{meta.name}=<value>"])
    on_screen = meta.id in screen
    return Check(
        check_id,
        "fail",
        f"yojo.yaml requires {meta.shown} to have a value before {issue.key} moves on; it is "
        "empty.",
        f"Add --set '{meta.name}=<value>'." if on_screen else f"Set it first with {update}.",
        {"field_id": meta.id},
        Edit("set", meta.name, placeholder(), "missing") if on_screen else None,
    )


def requirements(
    client: ServiceClient,
    jira: JiraConfig,
    key: str,
    to: str,
    requested: list[Assignment],
    users: UserLookup,
) -> Checklist:
    return evaluate(jira, to, gather(client, jira, key, to), requested, users)


# --------------------------------------------------------------------------- executor


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


def _failed_status(exc: YojoError) -> str:
    return "rejected" if exc.write_outcome == "rejected" else "not_attempted"


def recovery_argv(key: str, to: str, sets: list[str]) -> dict[str, list[str]]:
    """Inspection commands only; a write with an unknown or partial outcome gets no retry."""
    return {
        "inspect": [PROGRAM, "jira", "issue", "get", key],
        "requirements": [
            PROGRAM,
            "jira",
            "requirements",
            key,
            "--to",
            to,
            *[token for item in sets for token in ("--set", item)],
        ],
    }


def _inspect_fix(key: str, to: str, sets: list[str], what: str) -> str:
    argv = recovery_argv(key, to, sets)
    return (
        f"Do not retry yet. {what} Inspect {key} with `{posix(argv['inspect'])}` and compare its "
        f"status and fields with what you sent. Only if the change is not there, confirm "
        f"`{posix(argv['requirements'])}` reports ready: true before running the transition "
        f"again. yojo did not retry. {POSIX_NOTE}"
    )


def transition(
    client: ServiceClient,
    jira: JiraConfig,
    key: str,
    to: str,
    requested: list[Assignment],
    users: UserLookup,
    sets: list[str],
) -> dict[str, Any]:
    checklist = requirements(client, jira, key, to, requested, users)
    if not checklist.ready:
        raise refusal(
            f"Moving {key} to {checklist.to!r}",
            checklist.checks,
            {"key": key, "to": checklist.to, "from_status": checklist.from_status},
        )
    live = checklist.transition
    assert live is not None
    edit_fields = checklist.edit_fields
    edit_done = False
    client.announce(
        f"Moving {key} from {checklist.from_status!r} to {checklist.to!r}"
        + _changes(edit_fields, checklist.transition_fields)
    )

    if edit_fields:
        try:
            client.request(
                "PUT",
                f"/rest/api/2/issue/{seg(key)}",
                json_body={"fields": {f.id: f.payload for f in edit_fields}},
            )
        except YojoError as exc:
            if exc.write_outcome == "unknown":
                exc.details.update(
                    key=key,
                    atomic=False,
                    operations=_ops(
                        ("unknown", edit_fields),
                        ("not_attempted", checklist.transition_fields, live.id),
                    ),
                    recovery_argv=recovery_argv(key, checklist.to, sets),
                )
                exc.fix = _inspect_fix(
                    key, checklist.to, sets, "The edit before the transition may have been applied."
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
            refreshed = requirements(client, jira, key, to, requested, users)
        except YojoError as exc:
            raise _partial(
                key,
                checklist.to,
                sets,
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
                checklist.to,
                sets,
                edit_fields,
                refreshed.transition_fields,
                refreshed.transition.id if refreshed.transition else None,
                "skipped",
                "requirements no longer pass after the edit",
                {"checks": [c.as_dict() for c in failed(refreshed.checks)]},
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
    except YojoError as exc:
        if exc.write_outcome == "unknown":
            exc.details.update(
                key=key,
                atomic=not edit_done,
                operations=_ops(
                    edit_state,
                    ("unknown", checklist.transition_fields, live.id),
                ),
                recovery_argv=recovery_argv(key, checklist.to, sets),
            )
            exc.fix = _inspect_fix(
                key, checklist.to, sets, "The transition may or may not have been applied."
            )
            raise
        if edit_done:
            raise _partial(
                key,
                checklist.to,
                sets,
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
                f"Jira rejected moving {key} after yojo's checks passed (likely a workflow "
                "validator Jira does not expose); nothing changed."
            )
        raise

    return {
        "key": key,
        "url": client.url(f"/browse/{seg(key)}"),
        "from_status": checklist.from_status,
        "status": checklist.to,
        "transition_id": live.id,
        "changed": [f.as_dict() for f in (*edit_fields, *checklist.transition_fields)],
        "atomic": not edit_done,
        "operations": _ops(edit_state, ("succeeded", checklist.transition_fields, live.id)),
    }


def _changes(*groups: list[PlannedField]) -> str:
    names = [f.name for group in groups for f in group]
    return f", setting {', '.join(names)}" if names else ""


def _partial(
    key: str,
    to: str,
    sets: list[str],
    edit_fields: list[PlannedField],
    transition_fields: list[PlannedField],
    transition_id: str | None,
    transition_status: str,
    reason: str,
    extra: dict[str, Any],
) -> YojoError:
    argv = recovery_argv(key, to, sets)
    return YojoError(
        "partial_transition",
        f"The edit of {key} was applied but the move to {to!r} was not: {reason}.",
        f"Do not roll back and do not retry yet. Inspect {key} with `{posix(argv['inspect'])}` "
        "(the edit may have overwritten a concurrent change), reconcile its fields with the "
        f"issue owner, and resolve the failed checks or remote errors. Only when "
        f"`{posix(argv['requirements'])}` reports ready: true, run the transition again; it "
        f"repeats the edit (PUT). {POSIX_NOTE}",
        {
            "key": key,
            "atomic": False,
            "recovery_argv": argv,
            "operations": _ops(
                ("succeeded", edit_fields),
                (transition_status, transition_fields, transition_id),
                reason,
            ),
            **extra,
        },
    )


def describe(value: Any) -> str:
    return sanitize_text(value, 60)
