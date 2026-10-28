# Configuration (`yojo.yaml`, version 1)

`yojo.yaml` is **optional**. With no file, every command works by passing the instance root with
`--base-url`, and `yojo` reads Jira's own metadata for issue types, required fields, screens, and
allowed values. A `yojo.yaml` adds three things on top:

- the instance URLs, so you can drop `--base-url`;
- friendly **aliases** for fields your team names its own way;
- extra **team rules**: fields required on create, and what a transition needs.

Keep it in a repository so changes are reviewed. Start from
[`yojo.example.yaml`](../yojo.example.yaml). Every value in it is a placeholder.

This page is the exact reference for every key. For annotated, ready-to-adapt examples, see the
[cookbook](admin-cookbook.md). For fixing `config check` results, see
[Troubleshooting](troubleshooting.md#config-drift).

## Draft it with `yojo init`

`yojo init` writes a starting `yojo.yaml` from live metadata, using the logins already stored for
each URL. It only lists what you name: the service URLs and the `--project` keys. It confirms each
URL answers with your login, each project exists, and each `--repo` exists in Bitbucket, then
shows the YAML. Version 1 has no repository list, so `--repo` is checked and reported but not
saved.

```bash
yojo --json init --jira-url https://jira.example.test/jira --project PROJ
yojo --json init --jira-url https://jira.example.test/jira --project PROJ --save
```

For an instance signed by a private CA, add `--ca-bundle certs/team-ca.pem` to both commands.
`init` checks that the PEM file exists, uses it for the live connection, and records it under
`ca_bundle` for each selected service. A relative path is resolved from the output file's folder.
The bundle contains public CA certificates, never a token or client private key.
For the first login, pass the same bundle to
`yojo login --service jira --base-url https://jira.example.test/jira --ca-bundle certs/team-ca.pem`.
Login resolves a relative bundle path from the current directory, so when `init` stops because
a login is missing, its `fix` names the bundle by its absolute path.

- Without `--save`, nothing is written. `existing_sha256` is the digest of any file already at the
  destination (`yojo.yaml` in the current directory, or `--output PATH`).
- `--save` creates the file only if it is absent, and reports `unchanged` when the file already
  matches the draft.
- `--save --replace --expected-sha256 HEX` replaces an existing file only while its digest still
  equals `HEX` at yojo's final check. Use the `existing_sha256` from the preview you reviewed.
  A different file detected at that check is refused with
  [`file_conflict`](reference/errors.md#file_conflict). The file's permissions are kept. Close other
  editors or processes that may write the same file during replacement: portable file operations
  cannot make the content check and replacement one indivisible step against an unrelated writer.
- A symbolic link at the destination or in a folder on its path is refused, and a lone `atl.yaml`
  next to the destination stops the save with `config_legacy`.

`yojo init` never infers team policy from Jira. To add aliases and rules, pass them as JSON with
`--policy-input FILE` (or `--policy-input -` for stdin). `--input` is an equivalent short form.
The document has one key, `jira`, holding the same
`fields` and `projects` shapes as this file; each `projects[].key` must also be given with
`--project`:

```json
{
  "jira": {
    "fields": {"points": {"jira_name": "Story Points"}},
    "projects": [
      {
        "key": "PROJ",
        "create": {"Bug": {"required": ["Severity"]}},
        "transitions": {
          "finish": {
            "from": ["In Progress"],
            "to": "Done",
            "require": [{"kind": "field_set", "field": "points"}]
          }
        }
      }
    ]
  }
}
```

Every field, issue type, and status in it is checked against live Jira, the same way as
`yojo config check`. A name Jira does not have, or one that matches more than one field, is
refused with `input_invalid` and the problems listed in `details.problems`, each with a
suggestion when one is clear. Nothing is written then.

## Discovery

`yojo` looks for `yojo.yaml` in the current directory, then in each parent directory up to the
filesystem root, and uses the first one it finds. `--config PATH` overrides discovery; that file
must exist. A relative path is resolved from the current directory. If no file is found, commands
run without one.

### Moving from `atl.yaml`

`yojo` does not read the old `atl.yaml` and never converts it. If discovery reaches a directory
that has an `atl.yaml` but no `yojo.yaml`, every command except `commands`, `schema`, and
`--help` stops with [`config_legacy`](reference/errors.md#config_legacy) (exit 2), even with
`--base-url`, and nothing is read or changed. To move:

1. Rename `atl.yaml` to `yojo.yaml`.
2. Keep `base_url`, `ca_bundle`, `max_attachment_bytes`, and `transitions`.
3. Keep `fields` entries but remove each `encoding`. Values are now matched against live Jira
   metadata.
4. Remove legacy workflow-specific keys. Use `create.<Issue Type>.required` for fields your
   team requires on create.
5. Run `yojo config check`.

Credentials stored by `atl` are not reused either: run `yojo login` once per instance.

## Parsing rules

- The file is parsed with a `yaml.SafeLoader` subclass and capped at 256 KiB. The loader also
  rejects:
  - duplicate keys
  - anchors, aliases, and merge keys
  - non-string keys
  - Python or other custom tags
- Only `true` and `false` are booleans. YAML 1.1 words such as `Yes`, `No`, `On`, and `Off` stay
  strings, so `auto_set: {Review Flag: Yes}` means the option value `"Yes"`.
- Unknown keys anywhere are errors. So are missing required keys and values of the wrong type.
  Every error is `config_invalid` (exit 2). The `details.where` field gives the exact key path.
- Credentials are never read from, or written to, this file.

## Reference

```yaml
version: 1                       # required; only 1 is supported
jira:                            # optional section
  base_url: https://jira.example.test/jira   # required in the section; https only
  ca_bundle: certs/team-ca.pem   # optional PEM file; relative to this file; must exist
  max_attachment_bytes: 52428800 # optional positive integer (bytes); default 50 MiB
  fields:                        # optional aliases for every project
    points: {jira_name: Story Points}        # exactly one of jira_name / id
    team: {id: customfield_10400}
  projects:                      # optional list
    - key: PROJ                  # uppercase project key; must be unique
      fields:                    # optional aliases for this project; override jira.fields
        team: {id: customfield_10410}
      create:                    # optional; issue type name -> extra required fields
        Bug:
          required: [Environment, team]      # field names, aliases, or IDs
      transitions:               # optional; rule name (snake_case) -> rule
        start:
          from: [To Do]          # source statuses (non-empty)
          to: In Progress        # destination status
          pre_edit: false        # optional strict boolean; default false
          auto_set: {Review Flag: "Yes"}    # field -> value set during the transition
          require: []            # extra requirements, see below
confluence:                      # optional section
  base_url: https://confluence.example.test/confluence
  ca_bundle: certs/team-ca.pem   # optional
bitbucket:                       # optional section
  base_url: https://bitbucket.example.test/bitbucket
  ca_bundle: certs/team-ca.pem   # optional
```

At least one of `jira`, `confluence`, or `bitbucket` must be present, so a file with only a
`bitbucket` section is valid.

### URLs and TLS

The base URL must use `https://`. It keeps any reverse-proxy context path (`/jira`), and a
trailing slash is removed. URLs with credentials, a query string, or a fragment are rejected. The
same rules apply to `--base-url`.

`--base-url` and `base_url` can both be given only when they name the same instance (after
normalization); otherwise the call is refused with `input_invalid` and nothing is sent. A stored
login belongs to one service and URL, so a conflicting pair could otherwise send one instance's
credential to another.

TLS always verifies the certificate and host name, with TLS 1.2 or later. With no `ca_bundle`,
`yojo` uses the operating system trust store (via `truststore`). With a `ca_bundle`, only that PEM
file is trusted. Point it at the team's internal CA when the servers use one. Verification cannot
be disabled.

`yojo` never follows redirects. A 3xx response returns `redirect_refused`. Set `base_url` to the
final URL. SSO redirects usually mean you should use a PAT.

### Fields and aliases

Anywhere a field name is accepted (`--set`, `--fields`, `auto_set`, `create.<Type>.required`,
`field_set`), you can use:

- the field's name as Jira shows it, in any letter case (`Story Points`, `story points`);
- a raw field ID (`customfield_10002`, `resolution`);
- an alias from `jira.fields` or the issue's project `fields`.

An alias is a lowercase snake_case name mapped to exactly one of:

- `jira_name`: the field's display name, resolved against live metadata where it is used (create
  screen, edit screen, transition screen, or Jira's field list). If two fields share the name, the
  check fails and lists the candidate IDs; use `id` to pick one.
- `id`: an explicit field ID, such as `customfield_10100` or `resolution`.

A project's alias overrides a global alias of the same name for that project's issues.

There are no encodings to configure. Values are matched against the live field type: plain text,
numbers, option labels (single and multi select, priority, resolution, version, component),
people, issue keys for link and parent fields, and dates. A field type `yojo` cannot set safely
fails with a field-specific fix instead of a guess. Disabled options are not allowed.

If two names supplied in one write resolve to the same Jira field ID, the write is refused before
anything is sent and names both.

### Create requirements

`create` maps an issue type name (any letter case; each type once) to a block with a `required`
list. Each listed field must be given a non-blank value whenever an issue of that type is created
in the project, on top of the fields Jira itself marks required. A listed field that is not on the
type's create screen fails with a fix to add it to the screen or correct `yojo.yaml`; it is never
skipped.

### Transitions

A rule applies when the project is configured and the transition's destination status equals the
rule's `to` (any letter case). With no matching rule, only Jira's own checks run.

- The issue's current status must be in the rule's `from`. Two rules cannot share a destination
  from the same source status.
- Jira must currently offer **exactly one** transition to that destination.
- `auto_set` values are set during the transition. Each must be valid for the field, and a caller's
  `--set` for the same field must match it. A field the transition screen requires (without a
  default) must be supplied by `auto_set` or by the caller's `--set`.
- `require` lists extra checks:
  - `kind: attachment_filename` with a basename glob in `pattern`. `{key}` is replaced with the
    issue key. `*`, `?`, and `[...]` work as in `fnmatch`, and matching is case-sensitive. Path
    separators and other placeholders are rejected. All existing attachment basenames are checked.
  - `kind: field_set` with a `field`. The field must have a value on the issue, or be set by this
    transition, before the issue moves. If the field is on the transition screen, the fix adds a
    `--set`; otherwise it says to set it first with `yojo jira issue update`.

### `pre_edit` (non-atomic)

Some teams want `auto_set` to set a field that is **not on the transition screen**:

- `pre_edit: false` (default): the check fails before any write and tells you to add the field to
  the screen or opt in.
- `pre_edit: true`: `yojo` reads `GET /rest/api/2/issue/{key}/editmeta`. The field must be present
  there, allow the `set` operation, and accept the configured value. The write sequence is then:
  1. One `PUT /rest/api/2/issue/{key}` containing **only** the off-screen fields.
  2. Re-read the issue and its transitions, and re-run every check.
  3. `POST /rest/api/2/issue/{key}/transitions` with the screen fields only.

  If every field is on the screen, no PUT is sent.

The PUT and POST are separate writes. They are **not atomic**:

| Situation | Result |
|-----------|--------|
| PUT rejected (4xx) | original error code (usually `remote_validation`, exit 5); edit `rejected`, transition `not_attempted` |
| PUT outcome unknown (timeout/disconnect/408/499/5xx) | `outcome_unknown`, exit 6; edit `unknown`, transition `not_attempted` |
| PUT succeeded, refresh fails or a check no longer passes | `partial_transition`, exit 7; edit `succeeded`, transition `skipped` |
| PUT succeeded, POST rejected | `partial_transition`, exit 7; edit `succeeded`, transition `rejected` |
| PUT succeeded, POST outcome unknown | `outcome_unknown`, exit 6; edit `succeeded`, transition `unknown` |
| both succeed | success with `atomic: false` and both operations `succeeded` |

`yojo` never rolls back the edit and never retries. Running the transition again sends the edit
(PUT) again unconditionally, which can overwrite a change someone made after the first edit. Before
running it again:

1. Inspect the issue's current status and the values of every field in the plan's `edit` operation
   (in the Jira issue view, or with `yojo jira issue get KEY --fields ...`).
2. Reconcile any value that differs from what `yojo` wrote with the issue owner.
3. Run `yojo jira requirements KEY --to STATUS` and continue only when it reports `ready: true`.

### `max_attachment_bytes`

This must be a positive integer number of bytes. Booleans, floats, strings, zero, and negative
values are rejected. Without a config the default is 50 MiB. A file at exactly the limit is
allowed; one byte more is refused before anything is sent. If the file grows after its size was
checked, the upload is aborted before the body completes, so no more than the declared size is
ever sent. The request may already have reached Jira, so this is `outcome_unknown` (exit 6); check
the issue view before running it again. Jira's own upload limit is also checked before the upload
when Jira reports it; a proxy may enforce a lower one, which shows up as `remote_limit_exceeded`
(exit 5).

## Drift checks

`yojo config check` is read-only and needs a `yojo.yaml`. When the file has a `bitbucket`
section, it checks that `bitbucket.base_url` answers as Bitbucket Data Center (`drift` if another
application answers) and that the stored token is accepted (`unverified` with a `yojo login` fix if
not). A Confluence-only file reports that there are no team rules to check. In a mixed file,
each configured service is checked independently, so an unavailable Jira instance does not hide
the Bitbucket result. When the file has a `jira` section, it compares the file with live Jira:

- Every alias in `jira.fields` and each project's `fields` resolves to exactly one field in Jira's
  field list (an `id` must exist; a `jira_name` must match one field).
- Each project exists and is visible to you.
- Each transition rule's `from` and `to` statuses exist in that project's workflows.
- Each field named in `auto_set` and in `field_set` requirements resolves to exactly one field.
- Each issue type under `create` can be created in the project, and each field in its `required`
  list resolves to exactly one field.

A near miss in any name comes with a "Did you mean ...?" suggestion. Each check is `pass`,
`drift`, or `unverified`. Exit 0 means everything passed. Any drift is
[`config_drift`](reference/errors.md#config_drift) (exit 4). Otherwise, any unverified check is
[`config_unverified`](reference/errors.md#config_unverified) (exit 4).

`config check` does not read transition or create screens for a particular issue. Those are
checked live by every write, which refuses with every problem listed before anything is sent.
