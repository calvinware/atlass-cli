<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/calvinware/yojo/main/docs/assets/banner-dark.svg">
  <img src="https://raw.githubusercontent.com/calvinware/yojo/main/docs/assets/banner-light.svg" width="800" alt="yojo: command line for Jira and Confluence Data Center">
</picture>

[![CI](https://github.com/calvinware/yojo/actions/workflows/ci.yml/badge.svg)](https://github.com/calvinware/yojo/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://github.com/calvinware/yojo/blob/main/pyproject.toml)
[![Platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20macOS%20%7C%20Linux-555)](https://github.com/calvinware/yojo/blob/main/docs/getting-started.md)

</div>

# yojo

`yojo` is a command line for Jira, Confluence, and Bitbucket Data Center. It accepts the field
names, statuses, option labels, and people you see in Jira, resolves IDs itself, and checks the
rules before every write. A `yojo.yaml` can add your team's aliases and rules, but is optional.

## Get started

Until the first release is published, [install from source](https://github.com/calvinware/yojo/blob/main/docs/getting-started.md#from-source-contributors).
After publication, install with `pipx install yojo` or `uv tool install yojo` (Python 3.11+).
A person then runs
`yojo login --service jira --base-url https://jira.example.test/jira`; the credential goes to
the native OS keyring. Try a read:

```bash
yojo --json jira issue get PROJ-123 --base-url https://jira.example.test/jira
```

To create an issue, supply the fields Jira asks for. Field and option names are resolved during
the same call:

```bash
yojo --json jira issue create PROJ --type Bug --summary "Login fails on Safari" \
  --set Severity=Major --base-url https://jira.example.test/jira
```

The response is one compact JSON object when piped or with `--json`. A terminal without
`--json` shows a readable summary of the same result. A refused write lists every problem,
says nothing was sent, and provides a corrected `retry` command. If the server cannot confirm a
write, inspect the remote item before trying again. `yojo` never retries a write automatically.

Opening a pull request is one call too; the branches are checked and an existing open pull
request for the same branches is refused:

```bash
yojo --json bitbucket pr create --repo PROJ/web --source feature/login --target main \
  --title "Fix login on Safari" --base-url https://bitbucket.example.test/bitbucket
```

Offline discovery works without config or credentials:

```bash
yojo --json commands
yojo --json schema 'jira issue create'
```

## Documentation

| Audience | Start here |
|----------|------------|
| Developers | [Getting started](https://github.com/calvinware/yojo/blob/main/docs/getting-started.md), [Jira workflows](https://github.com/calvinware/yojo/blob/main/docs/guides/jira-workflows.md), [Confluence pages](https://github.com/calvinware/yojo/blob/main/docs/guides/confluence.md), [Bitbucket pull requests](https://github.com/calvinware/yojo/blob/main/docs/guides/bitbucket.md) |
| AI agents | [AGENTS.md](https://github.com/calvinware/yojo/blob/main/AGENTS.md), [Agent guide](https://github.com/calvinware/yojo/blob/main/docs/agents.md) |
| Admins | [Configuration](https://github.com/calvinware/yojo/blob/main/docs/configuration.md), [Cookbook](https://github.com/calvinware/yojo/blob/main/docs/admin-cookbook.md) |
| Contributors | [Contributing](https://github.com/calvinware/yojo/blob/main/docs/contributing.md), [Security](https://github.com/calvinware/yojo/blob/main/docs/security.md) |
| Everyone | [Command reference](https://github.com/calvinware/yojo/blob/main/docs/reference/commands.md), [Error catalog](https://github.com/calvinware/yojo/blob/main/docs/reference/errors.md), [FAQ](https://github.com/calvinware/yojo/blob/main/docs/faq.md), [Troubleshooting](https://github.com/calvinware/yojo/blob/main/docs/troubleshooting.md) |

## Compatibility

`yojo` targets Jira Data Center 9+, Confluence Data Center 7.9+, and Bitbucket Data Center (REST
1.0; Bitbucket Cloud is not supported) on Windows, macOS, and Linux.
Its fixture tests do not establish live compatibility with every server version. The
[security guide](https://github.com/calvinware/yojo/blob/main/docs/security.md) describes TLS,
keyring, permissions, and uncertain-write handling.

### Live compatibility checklist

Record the exact server version when each path is checked against a disposable instance.
The fixture suite does not substitute for that check.

| Check | Jira version | Confluence version | Status |
|-------|--------------|--------------------|--------|
| PAT and basic login | — | — | unverified |
| Custom CA, context path, and redirect refusal | — | — | unverified |
| Issue create with live fields and required values | — | n/a | unverified |
| Issue update, link, and read-back confirmation | — | n/a | unverified |
| Transition with on-screen and `pre_edit` fields | — | n/a | unverified |
| Attachment permissions and size limits | — | n/a | unverified |
| JQL and CQL pagination | — | — | unverified |
| Confluence create, update, and stale-version conflict | n/a | — | unverified |

| Bitbucket check | Bitbucket version | Status |
|-----------------|-------------------|--------|
| Token login (application identity and signed-in check) | — | unverified |
| Repository, branch, and pull request list cursors | — | unverified |
| Pull request diff, including server-side truncation | — | unverified |
| Pull request create and comment, including duplicate refusal | — | unverified |
