# `yojo.yaml` cookbook

`yojo` works without a `yojo.yaml`: Jira itself already says which fields are required, which
transitions exist, and which values are allowed. A `yojo.yaml` is where your team adds its own
words and rules on top: instance URLs, friendly field names, fields you always want on create, and
what a transition needs. This page gives annotated recipes. The exact rules for every key are in
the [configuration reference](configuration.md).

> [!NOTE]
> Every complete recipe below (the blocks that start with `version: 1`) is loaded by the test
> suite with `yojo`'s real config parser, so they stay valid. Values such as URLs, project keys,
> field IDs, and statuses are placeholders.

## Workflow for admins

1. Start from [`yojo.example.yaml`](../yojo.example.yaml) and keep `yojo.yaml` in the repository
   root, so every change goes through code review.
2. Fill in real URLs, project keys, field names or IDs, and workflow statuses. Use the names Jira
   shows; any letter case works.
3. Run `yojo config check` against live Jira. Fix every `drift` and `unverified` check (see
   [Troubleshooting](troubleshooting.md)).
4. Re-run `yojo config check` after any Jira field, project, or workflow change.

> [!WARNING]
> Never put credentials in `yojo.yaml`. `yojo` never reads them from the file; people log in with
> `yojo login`, which uses the OS keyring.

## Three teams, three files

No setup is the "right" one. These three unrelated teams use the same `yojo` with different
rules. Each file is complete; copy the one closest to yours and change the names.

| | Small product team | Documentation team | Operations team |
|---|---|---|---|
| Services | Jira, Bitbucket | Jira, Confluence | Jira, Confluence, Bitbucket |
| Project | `APP` | `DOCS` | `OPS` |
| Friendly names | `points` | `page`, `audience` | `sev`, `service` |
| Required on create | Story: `points` | Task: `audience` | Incident: `sev`, `service`, Environment |
| Transition rules | Review needs `points`; Done needs Resolution | Publish needs `page` and a reviewer | Resolve needs Root Cause and a `{key}_Timeline_*` file |
| Attachments | Jira's limit | Jira's limit | 10 MiB cap |

### Small product team

Engineers create stories and bugs from the terminal and open pull requests in the same repository.
The only extra rule: a story is estimated before review.

```yaml
version: 1
jira:
  base_url: https://jira.example.test/jira
  fields:
    points: {jira_name: Story Points}
  projects:
    - key: APP
      create:
        Story:
          required: [points]
      transitions:
        review:
          from: [In Progress]
          to: In Review
          require:
            - kind: field_set
              field: points
        done:
          from: [In Review]
          to: Done
          require:
            - kind: field_set
              field: Resolution
bitbucket:
  base_url: https://bitbucket.example.test/bitbucket
```

```bash
yojo jira issue create APP --type Story --summary "Add an export button" --set points=3
yojo bitbucket pr create --repo APP/web --source feature/export --target main --title "Add export"
```

### Documentation team

Writers track pages in Jira and publish in Confluence. Every task names its audience, and a page
is published only once it is linked and has a reviewer.

```yaml
version: 1
jira:
  base_url: https://jira.example.test/jira
  fields:
    page: {jira_name: Doc Page}
    audience: {jira_name: Audience}
  projects:
    - key: DOCS
      create:
        Task:
          required: [audience]
      transitions:
        publish:
          from: [In Review]
          to: Published
          require:
            - kind: field_set
              field: page
            - kind: field_set
              field: Reviewer
confluence:
  base_url: https://confluence.example.test/confluence
```

```bash
yojo jira issue create DOCS --type Task --summary "Document the export button" --set audience=Admins
yojo jira transition DOCS-12 --to Published --set page=123456 --set Reviewer=writer2
```

### Operations team

On-call engineers open incidents with severity and service, and close them only with a root cause
and a timeline file attached. Files over 10 MiB are refused before upload.

```yaml
version: 1
jira:
  base_url: https://jira.example.test/jira
  max_attachment_bytes: 10485760               # 10 MiB
  fields:
    sev: {jira_name: Severity}
    service: {jira_name: Affected Service}
  projects:
    - key: OPS
      create:
        Incident:
          required: [sev, service, Environment]
      transitions:
        resolve:
          from: [Investigating, Mitigated]
          to: Resolved
          require:
            - kind: field_set
              field: Root Cause
            - kind: attachment_filename
              pattern: "{key}_Timeline_*"
confluence:
  base_url: https://confluence.example.test/confluence
bitbucket:
  base_url: https://bitbucket.example.test/bitbucket
```

```bash
yojo jira issue create OPS --type Incident --summary "Example sign-in errors" \
  --set sev=High --set service=Sign-in --set Environment=Production
yojo jira attachment add OPS-7 --file OPS-7_Timeline_2026-10-28.md
yojo jira transition OPS-7 --to Resolved --set "Root Cause=Expired example certificate"
```

The recipes below show one idea each.

## Recipe 1: just the URLs

The smallest useful file. Everyone in the repository can drop `--base-url`, and nothing else
changes: every write still checks Jira's own required fields and screens.

```yaml
version: 1
jira:
  base_url: https://jira.example.test/jira     # https only; keep the context path
confluence:
  base_url: https://confluence.example.test/confluence
```

## Recipe 2: friendly names and fields required on create

Aliases let people write `--set points=3` or `--set team=Platform`. When two Jira fields share a
display name, a name lookup fails and lists both IDs, so map the one you mean with `id`. Here
every Bug in `PROJ` must also name an environment and a team, even though Jira does not require
them.

```yaml
version: 1
jira:
  base_url: https://jira.example.test/jira
  fields:                                      # aliases for every project
    points: {jira_name: Story Points}
    team: {id: customfield_10400}              # two fields are called "Team"; pick by ID
  projects:
    - key: PROJ
      create:
        Bug:
          required: [Environment, team]        # Jira names, aliases, or IDs
        Story:
          required: [points]
```

A create that leaves any of them out is refused with every missing field listed and a retry line:

```bash
yojo jira issue create PROJ --type Bug --summary "Login fails on Safari" \
  --set "Environment=Safari 18" --set team=Platform
```

## Recipe 3: a standard flow with transition rules

Moving to In Progress sets a `Review Flag` option on the transition screen. Done needs a
resolution on the issue and a supporting attachment.

```yaml
version: 1
jira:
  base_url: https://jira.example.test/jira
  projects:
    - key: PROJ
      transitions:
        start:
          from: [To Do, Selected]
          to: In Progress
          auto_set: {Review Flag: Yes}        # YAML 1.1 "Yes" stays the string "Yes"
        finish:
          from: [In Progress, In Review]
          to: Done
          require:
            - kind: field_set                  # must have a value, or be set by this move
              field: Resolution
            - kind: attachment_filename
              pattern: "{key}_Evidence_*"   # {key} becomes the issue key
```

With this file, `yojo jira transition PROJ-123 --to Done --set resolution=Fixed` checks the
resolution, the attachment, and Jira's own screen rules in one call.

## Recipe 4: an off-screen field with `pre_edit`

`Review Category` is not on the Done transition screen, so the team opted in to `pre_edit`: `yojo`
edits the field first (PUT), re-checks, then transitions (POST).

```yaml
version: 1
jira:
  base_url: https://jira.example.test/jira
  max_attachment_bytes: 20971520               # 20 MiB; Jira or a proxy may be lower
  projects:
    - key: PROJ
      fields:
        review_category: {jira_name: Review Category}   # project-only alias
      transitions:
        done:
          from: [In Progress, In Review]
          to: Done
          pre_edit: true                       # strict boolean; opt-in only
          auto_set:
            resolution: Done                   # on the transition screen
            review_category: Approved               # off-screen: set by a separate PUT
          require:
            - kind: attachment_filename
              pattern: "{key}_Evidence_*.xml"
```

> [!WARNING]
> `pre_edit` writes are **not atomic**. If the transition fails after the edit, the edit stays,
> the result is `partial_transition` (exit 7), and running it again repeats the edit. Prefer adding
> the field to the transition screen. See
> [`pre_edit` (non-atomic)](configuration.md#pre_edit-non-atomic).

## Recipe 5: two projects with different words

Project aliases override global ones, so each project can map `team` to its own field.

```yaml
version: 1
jira:
  base_url: https://jira.example.test/jira
  fields:
    team: {jira_name: Team}
  projects:
    - key: APP
      transitions:
        start:
          from: [Backlog, Selected]
          to: In Progress
    - key: OPS
      fields:
        team: {id: customfield_10410}          # OPS uses a different "Team" field
      create:
        Incident:
          required: [team, Severity]
```

## Excerpts

Internal CA and a reverse-proxy context path. The CA file path is relative to `yojo.yaml` and
must exist:

```yaml
jira:
  base_url: https://tools.example.test/jira
  ca_bundle: certs/team-ca.pem
confluence:
  base_url: https://tools.example.test/confluence
  ca_bundle: certs/team-ca.pem
```

Attachment name rules use `fnmatch` globs on the file's base name, case-sensitive:

```yaml
require:
  - kind: attachment_filename
    pattern: "{key}_Evidence_*"        # PROJ-42_Evidence_2026-10-24.xml
  - kind: attachment_filename
    pattern: "{key}_Coverage_[0-9]*.html" # PROJ-42_Coverage_87.html
```

## Values

There are no encodings to configure. `auto_set` values and `--set` values are written the way Jira
shows them, and `yojo` matches them against the field's live type:

| Field type | Write | Example |
|------------|-------|---------|
| text | the text | `Firefox on Linux` |
| number | a number | `3` |
| single select, priority, resolution, version, component | the label | `Done` |
| multi-value (labels, versions, components, multi select) | a YAML list, or comma-separated on the command line | `[Linux, Windows]` |
| person | username, display name, or email | `builder1` |
| issue link, parent, epic link | an issue key | `PROJ-10` |
| date | `YYYY-MM-DD` | `2026-10-28` |

If the live type or allowed values cannot confirm a safe value, `yojo` fails with a
field-specific fix instead of guessing.

## Drift

`yojo config check` compares the file with live Jira and reports each check as `pass`, `drift`,
or `unverified`. Drift is [`config_drift`](reference/errors.md#config_drift) (exit 4); unverified
without drift is [`config_unverified`](reference/errors.md#config_unverified) (exit 4). Common
causes and fixes are in [Troubleshooting](troubleshooting.md#config-drift).
