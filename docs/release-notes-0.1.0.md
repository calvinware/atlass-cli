# yojo 0.1.0 release notes (draft)

> [!IMPORTANT]
> **Status: draft. Not tagged, not published.** `yojo` 0.1.0 is not on PyPI yet, and no `v0.1.0`
> tag exists. Nothing on this page is a claim that a release happened. A repository owner decides
> when to tag; see [Release gates](#release-gates).

`yojo` is a generic command line for Jira, Confluence, and Bitbucket Data Center that an agent or
a person can use in one call per task. 0.1.0 is the first release under the name `yojo`; it
replaces `atlass-cli` 1.0.0, which stays on PyPI unchanged. The full list of changes is in the
[changelog](../CHANGELOG.md).

## Highlights

- **Generic Jira commands** (#14): `jira issue create|get|update|link`, transitions, comments,
  attachments, and search, with flags-first writes, `--set NAME=VALUE` in Jira's own words, and
  every rule and required field checked inside the same call. A refused write lists every problem
  with its `fix`, sends nothing, and returns the corrected `retry` command when `yojo` can build
  one. Works without a `yojo.yaml`.
- **Two faces, one result**: a readable terminal view, and one compact JSON object when piped or
  with `--json`, with identical exit codes.
- **Bitbucket Data Center** (#15): repository, branch, and pull request reads; pull request
  create and comment with preflight checks. Approving and merging stay with people.
- **Live `init` and agent instructions** (#16): `yojo init` drafts `yojo.yaml` from live metadata
  and saves only with `--save`; `yojo agent install --target agents|claude|cursor|copilot|gemini`
  writes instructions generated from the command registry, under 12 KiB.
- **Setup app** (#18): `yojo setup` walks a person through seven steps in a Textual terminal app,
  from connecting to a first read-only Try. Every step has a non-interactive command.
- **Documentation** (#17): a visual README, fixture-recorded demo, setup screenshots in both
  themes, exact command output in the reference, a side-by-side cookbook, and a Pages site.

## Breaking changes

- The package, import, and command are `yojo`. The `atl` command and `atlass_cli` module are gone.
- The config file is `yojo.yaml` at `version: 1`, and it is optional. A lone `atl.yaml` is refused
  with `config_legacy` and a rename-and-review fix; it is never migrated automatically.
- Workflow-specific commands and reserved fields are removed with no aliases.
- Credentials live under the keyring service `yojo`; run `yojo login` again.

## Compatibility

- **Data Center only**: Jira Data Center 9+, Confluence Data Center 7.9+, and Bitbucket Data
  Center (REST 1.0). Atlassian Cloud is not supported for any of the three.
- Python 3.11 or later on Windows, macOS, and Linux. CI tests 3.11, 3.13, 3.14, and 3.15 on all
  three systems.
- **Fixture-tested, not live-verified.** The test suite, the reference output, the screenshots,
  and the demo run against a fixture server that models documented Data Center responses. Every
  row of the README's [live compatibility checklist](../README.md#live-compatibility-checklist)
  is still `unverified`.

## Install (after publication)

Once 0.1.0 is published to PyPI, and not before:

```bash
pipx install yojo
# or
uv tool install yojo
```

Until then, install from source as described in
[Getting started](getting-started.md#from-source-contributors).

## Release gates

Each gate is separate evidence; one passing does not imply another.

| Gate | Owner | State at drafting |
|------|-------|-------------------|
| Review and merge in order: #19 (#14), #20 (#15), #21 (#16), #22 (#18), then the #17 pull request | maintainers | open |
| CI passes for the exact merge commit on `main`, including the OS matrix and docs checks | CI | pending merge |
| Pages source set to **GitHub Actions** so `.github/workflows/pages.yml` can deploy | repository owner | not verified |
| PyPI pending Trusted Publisher for `yojo` with workflow `release.yml` and environment `pypi` | repository owner | not verified |
| `pypi` GitHub environment with required reviewers | repository owner | not verified |
| `main` protected with required reviews and the `CI` check; `v*` tag ruleset | repository owner | not verified |
| Owner's explicit go to tag `v0.1.0` | repository owner | not given |

When every gate holds, the owner tags the reviewed merge commit. The `Release` workflow's
`verify` job then refuses unless the tag is exactly `v0.1.0`, the commit is on the first-parent
history of `main`, and the `CI` push run for that exact commit succeeded. Its `publish` job waits
for a human approval on the `pypi` environment and uploads with OIDC; no API token is involved.
The step-by-step procedure is in [Contributing › Release](contributing.md#release).

## After publication

- Confirm one wheel and one sdist on PyPI, and that the project page shows the README.
- In a clean shell: `pipx install yojo`, then `yojo --json commands` (its `data.version` is
  `0.1.0`) and `yojo schema 'jira issue create'`.
- Update the install wording in the README and `docs/getting-started.md` to say the package is
  published.
