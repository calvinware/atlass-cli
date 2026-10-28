"""The terminal view: a friendly, bounded rendering of the same envelope JSON mode prints.

Nothing here decides an outcome; it only presents the envelope (and the exit code is fixed
before rendering). Every string passes through ``clean`` so remote text cannot inject terminal
control sequences. Color is sparse and off when ``NO_COLOR`` is set or the terminal is dumb.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from yojo.transport import sanitize_text

MAX_CELL = 60
MAX_ROWS = 50
MAX_LINE = 300
WRITE_VERBS = {
    "jira issue create": "Created",
    "jira issue update": "Updated",
    "jira issue link": "Linked",
    "jira transition": "Moved",
    "jira comment add": "Commented on",
    "jira attachment add": "Attached to",
    "confluence page create": "Created page",
    "confluence page update": "Updated page",
    "bitbucket pr create": "Opened pull request",
    "bitbucket pr comment": "Commented on pull request",
}
PR_STATES = {"OPEN": "open", "MERGED": "merged", "DECLINED": "declined", "ALL": ""}
DIFF_LINES = 400
EMPTY_STATES = {
    "issues": "No issues matched this search.",
    "results": "No pages matched this search.",
    "fields": "",
    "changed": "",
    "attachments": "No attachments were confirmed.",
    "checks": "No checks to show.",
    "repos": "No repositories to show.",
    "branches": "No branches matched.",
    "files": "No changed files.",
}
TABLE_COLUMNS = {
    "issues": ["key", "status", "issue_type", "assignee", "summary"],
    "results": ["id", "space_key", "version", "title"],
    "commands": ["path", "summary"],
    "checks": ["status", "id", "message"],
    "fields": ["field", "value"],
    "changed": ["field", "id"],
    "attachments": ["filename", "size", "id"],
    "services": ["service", "status", "user", "base_url"],
    "operations": ["op", "method", "status", "transition_id"],
    "repos": ["repo", "name"],
    "branches": ["name", "default", "commit"],
    "pull_requests": ["id", "state", "source", "target", "author", "title"],
    "files": ["change", "path", "added", "removed"],
    "reviewers": ["name", "status"],
}
HIDDEN = {
    "content_trust",
    "url",
    "more",
    "next_start",
    "help",
    "exit_codes",
    "global_options",
    "diff",
    "truncated_files",
}


def color_enabled(environ: Mapping[str, str]) -> bool:
    return not environ.get("NO_COLOR") and environ.get("TERM") != "dumb"


class Style:
    def __init__(self, color: bool) -> None:
        self.color = color

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)


def clean(value: Any, limit: int = MAX_LINE) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, dict)):
        if isinstance(value, list) and all(isinstance(v, (str, int, float)) for v in value):
            return sanitize_text(", ".join(str(v) for v in value), limit)
        return sanitize_text(json.dumps(value, ensure_ascii=True), limit)
    return sanitize_text(str(value), limit)


def _label(key: str) -> str:
    return key.replace("_", " ").capitalize()


def table(rows: list[dict[str, Any]], columns: list[str], style: Style) -> list[str]:
    shown = rows[:MAX_ROWS]
    cells = [[clean(row.get(c), MAX_CELL) for c in columns] for row in shown]
    widths = [max([len(_label(c)), *(len(r[i]) for r in cells)]) for i, c in enumerate(columns)]
    head = "  ".join(_label(c).ljust(w) for c, w in zip(columns, widths, strict=True))
    lines = [style.bold(head.rstrip())]
    for raw, row in zip(shown, cells, strict=True):
        padded = [
            _tint(raw.get(c), v.ljust(w), style) if c == "status" else v.ljust(w)
            for c, v, w in zip(columns, row, widths, strict=True)
        ]
        lines.append("  ".join(padded).rstrip())
    if len(rows) > MAX_ROWS:
        lines.append(style.dim(f"... {len(rows) - MAX_ROWS} more (use --json to see all)"))
    return lines


def _tint(status: Any, text: str, style: Style) -> str:
    if status in ("pass", "stored", "succeeded"):
        return style.green(text)
    if status in ("fail", "drift", "failed", "rejected"):
        return style.red(text)
    if status in ("unverified", "unknown"):
        return style.yellow(text)
    return text


def _pairs(data: Mapping[str, Any], skip: set[str]) -> list[str]:
    scalars = [
        (k, v)
        for k, v in data.items()
        if k not in skip and k not in HIDDEN and not isinstance(v, (list, dict))
    ]
    if not scalars:
        return []
    width = max(len(_label(k)) for k, _ in scalars)
    return [f"{_label(k).ljust(width)}  {clean(v)}" for k, v in scalars]


def _lists(data: Mapping[str, Any], style: Style) -> list[str]:
    lines: list[str] = []
    for key, value in data.items():
        if key in HIDDEN or not isinstance(value, list):
            continue
        if not value:
            empty = EMPTY_STATES.get(key, "")
            if empty:
                lines += ["", empty]
            continue
        if all(isinstance(v, dict) for v in value):
            columns = TABLE_COLUMNS.get(key) or list(value[0])[:5]
            lines += ["", style.bold(_label(key)), *table(value, columns, style)]
    return lines


def _help(help_: Mapping[str, Any], style: Style) -> list[str]:
    lines = [style.bold(clean(help_.get("path"))) + "  " + clean(help_.get("summary"))]
    commands = help_.get("commands") or []
    if commands:
        lines += ["", *table(commands, ["usage", "summary"], style)]
    options = help_.get("global_options") or {}
    if options:
        lines += ["", style.bold("Options for every command")]
        width = max(len(k) for k in options)
        lines += [f"{k.ljust(width)}  {clean(v)}" for k, v in options.items()]
    note = help_.get("note")
    if note:
        lines += ["", style.dim(clean(note))]
    return lines


def _success(data: Any, command: str | None, style: Style) -> list[str]:
    if not isinstance(data, dict):
        return [clean(data)]
    if isinstance(data.get("help"), dict):
        return _help(data["help"], style)
    if command == "init":
        return _init(data, style)
    if command == "setup":
        return _setup(data, style)
    if command == "agent print":
        return [clean(line, 2000) for line in str(data.get("text") or "").splitlines()]
    if command == "agent install":
        files = data.get("files") or []
        head = style.green(f"Installed yojo instructions for {len(files)} tool(s).")
        return [head, "", *table(files, ["target", "action", "path"], style)]
    lines: list[str] = []
    verb = WRITE_VERBS.get(command or "")
    subject = data.get("key") or data.get("id")
    if command and command.startswith("bitbucket pr ") and subject:
        subject = f"#{subject} in {clean(data.get('repo'))}"
    if command in ("bitbucket pr create", "bitbucket pr get") and subject:
        lead = style.green(f"{verb} {subject}") if verb else style.bold(subject)
        lines.append(lead + f": {clean(data.get('title'), 80)}")
    elif command == "bitbucket pr diff":
        return _diff(data, style)
    elif command == "bitbucket pr list" and data.get("pull_requests") == []:
        state = PR_STATES.get(str(data.get("state")), "")
        lines.append(
            f"No {state + ' ' if state else ''}pull requests in {clean(data.get('repo'))}."
        )
    elif verb and subject:
        title = data.get("title") or data.get("summary")
        lines.append(
            style.green(f"{verb} {clean(subject)}")
            + (f": {clean(title, 80)}" if title and command and "page" in command else "")
        )
    elif command == "jira issue get" and subject:
        lines.append(style.bold(clean(subject)) + "  " + clean(data.get("summary"), 120))
    elif command == "commands":
        lines.append(style.bold(f"yojo {clean(data.get('version'))}") + " commands")
        lines += ["", *table(data.get("commands") or [], ["usage", "summary"], style)]
        lines += [
            "",
            style.dim(
                "Add --json for the machine-readable list; `yojo schema 'PATH'` "
                "shows one command's full contract."
            ),
        ]
        return lines
    elif command == "schema":
        lines += [
            style.bold(clean(data.get("command"))) + "  " + clean(data.get("summary")),
            "",
            f"Usage    {clean(data.get('usage'))}",
            f"Example  {clean(data.get('example'))}",
            "",
            style.dim("Add --json for the argument, input, success, and error JSON Schemas."),
        ]
        return lines
    elif command == "jira requirements":
        ready = data.get("ready")
        lines.append(
            style.green(f"{clean(data.get('key'))} is ready to move to {clean(data.get('to'))}.")
            if ready
            else style.red(
                f"{clean(data.get('key'))} is not ready to move to {clean(data.get('to'))}."
            )
        )
    lines += _pairs(
        data, {"key", "id", "title"} if verb or command == "bitbucket pr get" else set()
    )
    if isinstance(data.get("plan"), dict):
        lines += _lists({"operations": data["plan"].get("operations") or []}, style)
    lines += _lists(data, style)
    fixes = [
        c
        for c in data.get("checks") or []
        if isinstance(c, dict)
        and c.get("fix")
        and c.get("status") not in ("pass", "not_applicable")
    ]
    if fixes:
        lines += ["", style.bold("To fix")] + [f"- {clean(c['fix'])}" for c in fixes[:MAX_ROWS]]
    if data.get("url"):
        lines += ["", f"Open: {clean(data['url'], 2048)}"]
    if data.get("more"):
        lines.append(f"Full text: {clean(data['more'])}")
    if data.get("next_start") is not None:
        lines.append(f"Next page: run the same command with --start {clean(data['next_start'])}")
    return lines


INIT_HEADS = {
    "preview": "Draft yojo.yaml (not saved) for",
    "created": "Saved",
    "replaced": "Replaced",
    "unchanged": "Already up to date:",
}


def _init(data: Mapping[str, Any], style: Style) -> list[str]:
    action = str(data.get("action"))
    head = f"{INIT_HEADS.get(action, action)} {clean(data.get('path'), 1000)}"
    lines = [style.bold(head) if action == "preview" else style.green(head), ""]
    lines += [style.dim(clean(line)) for line in str(data.get("yaml") or "").splitlines()]
    services = data.get("services") or []
    columns = ["service", "user", "version", "base_url"]
    lines += ["", style.bold("Checked"), *table(services, columns, style)]
    for key, columns in (("projects", ["key", "name"]), ("repos", ["repo", "name"])):
        if data.get(key):
            lines += ["", *table(data[key], columns, style)]
    lines += [""] + [clean(note, 1000) for note in data.get("notes") or []]
    steps = data.get("next") or []
    if steps:
        lines += ["", style.bold("Next")] + [f"  {clean(step, 2000)}" for step in steps]
    return lines


def _setup(data: Mapping[str, Any], style: Style) -> list[str]:
    completed = data.get("status") == "completed"
    lines = [
        style.green("Setup finished. Fair winds.")
        if completed
        else style.bold("Setup closed before the last step.")
    ]
    config = data.get("config")
    if isinstance(config, dict):
        path, action = clean(config.get("path"), 1000), clean(config.get("action"))
        lines.append(f"yojo.yaml: {path} ({action})")
    else:
        lines.append("yojo.yaml was not changed.")
    stored = data.get("logins_stored") or []
    if stored:
        lines.append("Logins stored in your OS keyring: " + ", ".join(clean(s) for s in stored))
    for row in data.get("agent_files") or []:
        if isinstance(row, dict):
            path, action = clean(row.get("path")), clean(row.get("action"))
            lines.append(f"Agent instructions: {path} ({action})")
    lines += [clean(note, 1000) for note in data.get("notes") or []]
    steps = data.get("next") or []
    if steps:
        lines += ["", style.bold("Next")] + [f"  {clean(step, 2000)}" for step in steps]
    return lines


def _diff(data: Mapping[str, Any], style: Style) -> list[str]:
    """Pull request diff: changed files, then the diff lines, then whether anything was cut."""
    lines = [style.bold(f"Pull request #{clean(data.get('id'))} in {clean(data.get('repo'))}")]
    lines += _lists({"files": data.get("files") or []}, style)
    total, listed = data.get("files_total"), len(data.get("files") or [])
    if isinstance(total, int) and total > listed:
        lines.append(style.dim(f"... {total - listed} more changed files"))
    text = data.get("diff")
    # Split on newlines only: splitlines() would also break on separators inside source text.
    body = text.removesuffix("\n").split("\n") if isinstance(text, str) and text else []
    if body:
        lines.append("")
    for raw in body[:DIFF_LINES]:
        line = clean(raw, MAX_LINE)
        if raw.startswith("+") and not raw.startswith("+++"):
            line = style.green(line)
        elif raw.startswith("-") and not raw.startswith("---"):
            line = style.red(line)
        elif raw.startswith("@@"):
            line = style.dim(line)
        lines.append(line)
    if len(body) > DIFF_LINES:
        lines.append(style.dim(f"... {len(body) - DIFF_LINES} more lines (use --json to see all)"))
    if data.get("complete") is False:
        cut = []
        if data.get("truncated"):
            cut.append("yojo shortened this diff")
        if data.get("server_truncated"):
            cut.append("Bitbucket cut part of the diff")
        lines += ["", style.yellow("Incomplete: " + "; ".join(cut) + ".")]
        if data.get("more"):
            lines.append(f"More: {clean(data['more'])}")
        if data.get("inspect"):
            lines.append(f"In a clone with both commits: {clean(data['inspect'])}")
    if data.get("url"):
        lines += ["", f"Open: {clean(data['url'], 2048)}"]
    return lines


def _error(error: Mapping[str, Any], style: Style) -> list[str]:
    lines = [style.red("Error: ") + clean(error.get("message"), 1000)]
    raw_details = error.get("details")
    details: Mapping[str, Any] = raw_details if isinstance(raw_details, dict) else {}
    checks = [c for c in details.get("checks") or [] if isinstance(c, dict)]
    if checks:
        lines += ["", *table(checks, ["status", "id", "message"], style)]
        fixes = [c["fix"] for c in checks if c.get("fix")]
        if fixes:
            lines += ["", style.bold("To fix")] + [f"- {clean(f, 1000)}" for f in fixes]
    lines += ["", style.bold("Next: ") + clean(error.get("fix"), 2000)]
    if details.get("retry"):
        lines.append(style.bold("Retry: ") + clean(details["retry"], 2000))
    recovery = details.get("recovery_argv")
    if isinstance(recovery, dict):
        for name, argv in recovery.items():
            if isinstance(argv, list):
                lines.append(f"{_label(name)}: " + clean(" ".join(map(str, argv)), 1000))
    return lines


def render(envelope: Mapping[str, Any], command: str | None, style: Style) -> str:
    if envelope.get("ok") is True:
        lines = _success(envelope.get("data"), command, style)
    else:
        error = envelope.get("error")
        lines = _error(error if isinstance(error, dict) else {}, style)
    return "\n".join(lines).rstrip() + "\n"
