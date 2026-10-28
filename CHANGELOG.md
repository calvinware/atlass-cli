# Changelog

## 0.1.0 (unreleased)

First release under the name `yojo`. It replaces `atlass-cli` 1.0.0, which stays on PyPI as is.
Not yet tagged or published; see the [draft release notes](docs/release-notes-0.1.0.md).

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
- In a terminal with no `yojo.yaml` found, bare `yojo` opens `yojo setup` instead of printing
  help. Piped or with `--json`, it still answers with one JSON envelope.

### Added

Generic Jira commands and the shared contract (#14):

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

Bitbucket Data Center (#15):

- `bitbucket repo list|get`, `bitbucket branch list`, and `bitbucket pr list|get|diff` reads with
  explicit cursors, using a Bitbucket token stored by `yojo login --service bitbucket`.
- `bitbucket pr create` and `bitbucket pr comment` check the repository, both branches, the title,
  and any open pull request or identical comment first, and refuse with nothing sent. A diff says
  `complete: false` whenever anything was cut.
- Uncertain pull request writes return `outcome_unknown` with inspection commands. There is no
  approve or merge command; those stay with people.

Live init and agent instructions (#16):

- `yojo init` drafts a version 1 `yojo.yaml` from live metadata with the stored logins. It
  previews by default, saves only with `--save`, and replaces a file only with
  `--replace --expected-sha256`. Team rules come only from `--input` and are checked live.
  `--ca-bundle PEM` on login and init supports private CA certificates during bootstrap and
  records the bundle in the draft.
- `yojo agent print` and `yojo agent install --target agents|claude|cursor|copilot|gemini`
  generate agent instructions from the command registry, with every command's signature and a
  `--json` example in under 12 KiB. Installs replace only the marked yojo section.
- The `file_conflict` error code (exit 2) for local files that changed or would be overwritten.

Setup app (#18):

- `yojo setup`, and bare `yojo` on a first run, open a seven-step Textual app in a terminal:
  Connect, Log in, Choose projects, Map fields and rules, Review and save, Connect your agent, and
  Try it. Tokens go from a masked field straight to the OS keyring; Save writes only the reviewed
  `yojo.yaml` and refuses when the file or Jira changed; setup completes only after a read-only Try
  on the saved file.
- The app is keyboard-only friendly, follows `NO_COLOR`, and switches light and dark with Ctrl+T.
  Piped, with `--json`, without a terminal on stdin, or with `TERM=dumb`, `yojo setup` starts
  nothing and returns `usage_error` (exit 2) with the step-by-step commands in `details.steps`.
- Textual is a runtime dependency. It is imported only when the app opens.

Documentation (#17):

- README and site lead with a generic no-config task, an animated demo recorded from a VHS tape
  against the test fixture server, Mermaid diagrams of the write loop, roles, and the three
  products, and one copyable `yojo agent install` line per tool.
- The setup guide shows every step of the app in light and dark captures.
- The command reference shows each command's call with its exact terminal and `--json` output,
  captured from deterministic fixtures. CI fails when the reference, the captures, the demo, or the
  setup screenshots are stale.
- The cookbook compares three unrelated team configurations side by side.
- The site is built for GitHub Pages from `main`.
