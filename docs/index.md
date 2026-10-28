# yojo

![yojo: command line for Jira, Confluence, and Bitbucket Data Center](assets/banner-light.svg#gh-light-mode-only)
![yojo: command line for Jira, Confluence, and Bitbucket Data Center](assets/banner-dark.svg#gh-dark-mode-only)

`yojo` is a command line for Jira, Confluence, and Bitbucket Data Center, built so that a developer,
a Jira admin, or an AI agent on any model can do a task in **one call**. It speaks Jira's words
(field names, statuses, option labels, people), resolves the IDs itself, checks every rule before it
writes, and reports every problem at once with the corrected command. It works with no config; a
`yojo.yaml` adds your team's friendly names and rules on top.

## Pick your path

| You are | Start here | Then |
|---------|-----------|------|
| **Developer** using `yojo` daily | [Getting started](getting-started.md) | [Jira workflows](guides/jira-workflows.md), [Confluence pages](guides/confluence.md), [Bitbucket pull requests](guides/bitbucket.md) |
| **AI agent** (or building one) | [Agent guide](agents.md) | [Command reference](reference/commands.md), [Error catalog](reference/errors.md) |
| **Team admin** owning `yojo.yaml` | [Cookbook](admin-cookbook.md) | [Configuration reference](configuration.md), [Troubleshooting](troubleshooting.md) |
| **Contributor** changing `yojo` | [Contributing](contributing.md) | [Security](security.md), [Command behavior](commands.md) |

## See it

![Terminal recording: the command yojo jira issue create PROJ --type Bug --summary 'Fix example sign-in' --base-url https://jira.example.test/jira is typed once; yojo checks the project's create screen, creates the Bug, and prints its key, status, and link.](assets/demos/one-call.gif)

<sub>Recorded with [a VHS tape](demos/one-call.tape) against yojo's test fixture server (`jira.example.test`), not a live instance.</sub>

Start with no config: a person logs in once, then any read works.

```bash
yojo login --service jira --base-url https://jira.example.test/jira   # hidden prompt, OS keyring
yojo setup                                                            # optional seven-step app
yojo jira issue get PROJ-123 --base-url https://jira.example.test/jira
```

## One call per task

Every write checks first, inside the same call, and is reported as done only when the server
confirms it:

```mermaid
flowchart TD
    Call["One call<br/>yojo --json jira issue create ..."] --> Check{"Every rule, required field,<br/>and permission OK?"}
    Check -->|"no"| Refused["Refused, exit 4<br/>nothing was sent<br/>every problem with its fix,<br/>plus the corrected retry"]
    Refused -->|"supply the missing content,<br/>run retry once"| Call
    Check -->|"yes"| Write["Write sent once"]
    Write --> Confirm{"Server confirms<br/>the change?"}
    Confirm -->|"yes"| Done["Done, exit 0<br/>key, link, what changed"]
    Confirm -->|"cannot tell"| Unknown["outcome_unknown, exit 6<br/>or partial_transition, exit 7<br/>no retry is offered"]
    Unknown --> Inspect["Inspect the issue, page,<br/>or pull request first"]
    Inspect -->|"not applied"| Call
    Inspect -->|"applied, or unsure"| Stop["Stop: use what is there,<br/>or ask a person"]
```

Creates, updates, links, transitions, comments, attachments, Confluence pages, and Bitbucket pull
requests all work this way. `yojo` never retries a write on its own.

## Who decides what

```mermaid
flowchart LR
    subgraph Person["Person"]
        P1["Logs in; the token<br/>stays in the OS keyring"]
        P2["Owns yojo.yaml<br/>team rules, reviewed"]
        P3["Approves and merges<br/>pull requests"]
    end
    subgraph Agent["Agent or script"]
        A1["Writes summaries,<br/>descriptions, comments"]
        A2["Runs one yojo --json call"]
        A3["Follows error.fix and retry;<br/>inspects before rerunning"]
    end
    subgraph Yojo["yojo"]
        Y1["Resolves names to IDs"]
        Y2["Checks every rule<br/>before the write"]
        Y3["Writes once and confirms;<br/>never retries"]
    end
    Person -->|"logs in, sets rules"| Yojo
    Agent -->|"one call"| Yojo
    Yojo -->|"one JSON object"| Agent
    Agent -->|"exit 3, or unsure"| Person
```

## Three Data Center products, one command line

```mermaid
flowchart LR
    Caller["Person, script,<br/>or agent"] --> CLI["yojo<br/>one JSON object per call"]
    Config["yojo.yaml, optional<br/>aliases and rules"] -.-> CLI
    Keyring["OS keyring<br/>one token per service and URL"] --> CLI
    CLI -->|"HTTPS, verified TLS,<br/>no redirects"| Jira["Jira Data Center<br/>issues, transitions,<br/>comments, search"]
    CLI --> Confluence["Confluence Data Center<br/>page read, search,<br/>create, update"]
    CLI --> Bitbucket["Bitbucket Data Center<br/>repositories, branches,<br/>pull requests"]
```

## What you get

- **Two faces, one contract.** In a terminal: aligned tables, plain-language status, and a link
  after every write. Piped or with `--json`: exactly one compact JSON object, with fixed
  [exit codes](reference/errors.md#exit-codes) in both modes.
- **Natural values.** `--set "Story Points=3" --set priority=High --set assignee=builder1`. Any
  letter case; near misses get "Did you mean ...?".
- **Compact reads.** `--fields` picks more fields, long text is trimmed with a `more` command, and
  lists return `next_start` for the next page.
- **Agent-ready.** `yojo agent install --target agents|claude|cursor|copilot|gemini` writes the
  instructions your tool reads; see the [agent guide](agents.md).
- **All problems at once.** A refused write lists every problem with a concrete `fix` and a
  `retry` line, and nothing is sent.
- **Works without config.** `--base-url` and a keyring login are enough. `yojo.yaml` adds aliases,
  fields required on create, and transition rules.
- **Offline discovery.** `yojo commands` and `yojo schema '<command>'` need no config or
  credentials.
- **Secrets in the OS keyring** only, with verified TLS and no redirects.

> [!NOTE]
> The automated tests model documented Data Center responses. They do not prove live
> compatibility; see the compatibility section in the repository README.

## Quick taste

```bash
yojo commands                                         # offline
yojo schema 'jira issue create'                       # offline
yojo jira issue get PROJ-123 --base-url https://jira.example.test/jira
yojo jira transition PROJ-123 --to Done --set resolution=Fixed
```
