# Changelog

## 0.1.0 (unreleased)

First release under the name `yojo`. It replaces `atlass-cli` 1.0.0, which stays on PyPI as is.

### Breaking changes

- Renamed the package, import, and command to `yojo`. The `atl` command and the `atlass_cli`
  module are gone.
- The config file is `yojo.yaml`, starting fresh at `version: 1`, and it is optional. A lone
  `atl.yaml` is refused with a rename-and-review fix; it is never migrated automatically.
- Removed workflow-specific commands, reserved fields, and create policies with no aliases.
  Use `jira issue create|get|update|link`.
- Credentials are stored under the keyring service `yojo`; run `yojo login` again.
- `config check --issue` is removed; `config check` compares aliases, statuses, and create rules
  with live Jira.

### Added

- Bitbucket Data Center repository, branch, and pull request reads; pull request creation and
  comments with preflight checks, bounded diff output, and inspection steps for uncertain writes.
- `jira issue create`, `get`, `update`, and `link`, with flags-first writes and repeatable
  `--set NAME=VALUE`. `--input -` stays available.
- Field, issue type, status, option, link type, and person names resolve case-insensitively from
  live Jira metadata, with candidates on ambiguity and a suggestion on a near miss.
- Every write checks all required fields, rules, and permissions first, refuses with every problem
  listed and nothing sent, and returns `details.retry`, the corrected command.
- A friendly terminal view of the same envelope; piped output and `--json` stay one-line JSON.
  `NO_COLOR` is honored.
- `--base-url` works without `yojo.yaml`.
- The `field_set` transition rule.
- Compact output: `--fields`, trimmed long text with a command to get the rest, and a default
  search limit of 20.
- `yojo init` drafts a version 1 `yojo.yaml` from live metadata with the stored logins. It
  previews by default, saves only with `--save`, and replaces a file only with
  `--replace --expected-sha256`. Team rules come only from `--input` and are checked live.
- `yojo agent print` and `yojo agent install --target agents|claude|cursor|copilot|gemini`
  generate agent instructions from the command registry, with every command's signature and a
  `--json` example in under 12 KiB. Installs replace only the marked yojo section.
- The `file_conflict` error code (exit 2) for local files that changed or would be overwritten.
