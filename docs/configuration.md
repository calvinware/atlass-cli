# Configuration (`atl.yaml`, version 1)

`atl.yaml` holds the team's fixed rules. Keep it in a repository so changes are reviewed.
Start from [`atl.example.yaml`](../atl.example.yaml). Every value in it is a placeholder.

This page is the exact reference for every key. For annotated, ready-to-adapt examples, see the
[cookbook](admin-cookbook.md). For fixing `config check` results, see
[Troubleshooting](troubleshooting.md#config-drift).

## Discovery

`atl` looks for `atl.yaml` in the current directory, then in each parent directory up to the
filesystem root, and uses the first one it finds. `--config PATH` overrides discovery. A relative
path is resolved from the current directory. Only `atl commands`, `atl schema`, and `--help` run
without a config.

## Parsing rules

- The file is parsed with a `yaml.SafeLoader` subclass and capped at 256 KiB. The loader also
  rejects:
  - duplicate keys
  - anchors, aliases, and merge keys
  - non-string keys
  - Python or other custom tags
- Only `true` and `false` are booleans. YAML 1.1 words such as `Yes`, `No`, `On`, and `Off` stay
  strings, so `auto_set: {work_started: Yes}` means the option value `"Yes"`.
- Unknown keys anywhere are errors. So are missing required keys and values of the wrong type.
  Every error is `config_invalid` (exit 2). The `details.where` field gives the exact key path.
- Credentials are never read from, or written to, this file.

## Reference

```yaml
version: 1                       # required; only 1 is supported
jira:                            # optional section; required for jira commands
  base_url: https://jira.example.test/jira   # https only; no userinfo, query, or fragment
  ca_bundle: certs/team-ca.pem   # optional PEM file; relative to this file; must exist
  max_attachment_bytes: 52428800 # optional positive integer (bytes); default 50 MiB
  projects:                      # required, non-empty list
    - key: EXAMPLE               # uppercase project key; must be unique
      allowed_issue_types: [Story, Feature]
      story_type: Story          # must be in allowed_issue_types
      feature_type: Feature      # must be in allowed_issue_types and differ from story_type
      feature_create_policy: human_confirmed   # or automatic; default human_confirmed
      fields:                    # friendly_name -> exactly one of jira_name / id, plus encoding
        acceptance_criteria: {jira_name: Acceptance Criteria, encoding: text}   # required
        feature_link: {jira_name: Parent Link, encoding: issue_key_string}      # required
        work_started: {id: customfield_10300, encoding: single_option}
      create:                    # optional; omitted blocks and lists mean no extra requirements
        story:
          required: [acceptance_criteria, feature_link]   # checked only by story create
        feature:
          required: []           # checked only by feature create; default empty
      transitions:               # name (snake_case) -> rule
        in_progress:
          from: [To Do]          # source statuses (non-empty)
          to: In Progress        # destination status passed as --to
          pre_edit: false        # optional strict boolean; default false
          auto_set: {work_started: Yes}   # friendly field -> value set during the transition
          require: []            # extra requirements, see below
confluence:                      # optional section; required for confluence commands
  base_url: https://confluence.example.test/confluence
  ca_bundle: certs/team-ca.pem   # optional
```

### URLs and TLS

The base URL must use `https://`. It keeps any reverse-proxy context path (`/jira`), and a
trailing slash is removed. URLs with credentials, a query string, or a fragment are rejected.

TLS always verifies the certificate and host name, with TLS 1.2 or later. With no `ca_bundle`,
`atl` uses the operating system trust store (via `truststore`). With a `ca_bundle`, only that PEM
file is trusted. Point it at the team's internal CA when the servers use one. Verification cannot
be disabled.

`atl` never follows redirects. A 3xx response returns `redirect_refused`. Set `base_url` to the
final URL. SSO redirects usually mean you should use a PAT.

### Fields and encodings

Each friendly name maps to exactly one Jira field, chosen with one of:

- `jira_name`: the field's display name. It is resolved against live metadata for that context
  (create screen, transition screen, or edit metadata). If two fields share the name, the check
  fails and lists the candidate IDs. Use `id` to disambiguate.
- `id`: an explicit field ID, such as `customfield_10100` or `resolution`.

`acceptance_criteria` and `feature_link` must exist in every project. Story create fills them from
the input's `acceptance_criteria` and `feature_key`.

### Create requirements

`create` has one optional block per issue type: `story` and `feature`. Each block has an optional
`required` list of friendly names defined under `fields`. An omitted block or list means an empty
list. No other keys are allowed, and the older flat `create.required` list is rejected with
`config_invalid` (`details.where` is the `create` path).

- Story create enforces only `create.story.required`. It always requires the caller's
  `acceptance_criteria` and `feature_key` as well, even when the list is empty.
- Feature create enforces only `create.feature.required`. It also follows
  `feature_create_policy`.
- Each listed field must have a non-blank value and must be on that issue type's create screen,
  whether or not Jira marks it required. If it is not on the screen, the preflight fails with a
  fix; the field is never skipped.
- Live Jira required fields without a default are enforced for both types, listed or not.

If two friendly names supplied in one create or transition resolve to the same Jira field ID, the
preflight fails before any write and names both friendly names and the field ID.

| Encoding | Live schema required | Payload sent |
|----------|----------------------|--------------|
| `text` | `type: string`, no allowed values | `"value"` |
| `issue_key_string` | `type: string` or `any` (for example Parent Link, Epic Link) | `"ABC-1"` |
| `issue_key_object` | `type: issuelink` or `issue` | `{"key": "ABC-1"}` |
| `single_option` | `type: option` with `allowedValues` | `{"id": "<option id>"}` (matched by `value`) |
| `multi_option` | `type: array`, `items: option` with `allowedValues` | `[{"id": ...}, ...]` (YAML list) |
| `resolution_option` | `type: resolution` with `allowedValues` | `{"id": "<resolution id>"}` (matched by `name`) |

If the live schema or allowed values cannot confirm a safe encoding, `atl` fails with a
field-specific fix rather than guess. Disabled options are not allowed.

### Transitions

- `--to STATUS` must equal a configured `to` (case-insensitive). The issue's current status must be
  in that rule's `from`. Two rules cannot share a destination from the same source status.
- Jira must currently offer **exactly one** transition to that destination.
- Required fields on the transition screen without a default must be covered by `auto_set`.
  Otherwise the checklist fails with a fix to add them. Transition values come only from
  `auto_set`.
- `require` supports `kind: attachment_filename` with a basename glob in `pattern`. `{key}` is
  replaced with the issue key. `*`, `?`, and `[...]` work as in `fnmatch`, and matching is
  case-sensitive. Path separators and other placeholders are rejected. All existing attachment
  basenames are checked.

### `pre_edit` (non-atomic)

Some teams want `auto_set` to set a field that is **not on the transition screen**:

- `pre_edit: false` (default): the checklist fails before any write and tells you to add the field
  to the screen or opt in.
- `pre_edit: true`: `atl` reads `GET /rest/api/2/issue/{key}/editmeta`. The field must be present
  there, allow the `set` operation, and accept the configured value. The write sequence is then:
  1. One `PUT /rest/api/2/issue/{key}` containing **only** the off-screen fields.
  2. Re-read the issue and its transitions, and re-run the full checklist.
  3. `POST /rest/api/2/issue/{key}/transitions` with the on-screen fields only.

  If every field is on the screen, no PUT is sent.

The PUT and POST are separate writes. They are **not atomic**:

| Situation | Result |
|-----------|--------|
| PUT rejected (4xx) | original error code (usually `remote_validation`, exit 5); edit `rejected`, transition `not_attempted` |
| PUT outcome unknown (timeout/disconnect/408/499/5xx) | `outcome_unknown`, exit 6; edit `unknown`, transition `not_attempted` |
| PUT succeeded, refresh fails or checklist no longer passes | `partial_transition`, exit 7; edit `succeeded`, transition `skipped` |
| PUT succeeded, POST rejected | `partial_transition`, exit 7; edit `succeeded`, transition `rejected` |
| PUT succeeded, POST outcome unknown | `outcome_unknown`, exit 6; edit `succeeded`, transition `unknown` |
| both succeed | success with `atomic: false` and both operations `succeeded` |

`atl` never rolls back the edit and never retries. Re-running the transition sends the edit (PUT)
again unconditionally, which can overwrite a change someone made after the first edit. Before any
retry:

1. Inspect the issue's current status and the values of every field in the plan's `edit` operation
   (in the Jira issue view).
2. Reconcile any value that differs from what `atl` wrote with the issue owner.
3. Run `atl jira requirements KEY --to STATUS` again and retry only when it reports `ready: true`.

### `max_attachment_bytes`

This must be a positive integer number of bytes. Booleans, floats, strings, zero, and negative
values are rejected. A file at exactly the limit is allowed; one byte more is refused before
anything is sent. If the file grows after its size was checked, the upload is aborted before the
body completes, so no more than the declared size is ever sent. The request may already have
reached Jira, so this is `outcome_unknown` (exit 6); check the issue view before retrying. Jira or a proxy may enforce a lower
limit. That shows up as `remote_limit_exceeded` (exit 5).

## Drift checks

`atl config check [--issue KEY]` is read-only. It compares the file with live Jira:

- Each `allowed_issue_types` entry must be creatable in its project.
- On the story create screen: `acceptance_criteria`, `feature_link`, and every field in
  `create.story.required`. On the feature create screen: every field in `create.feature.required`.
  Each must resolve to exactly one field on that screen, allow the `set` operation (when Jira
  lists operations), not share a Jira field ID with another listed name, and have a compatible
  schema. Check IDs name
  the type, for example `EXAMPLE:Story:field:feature_link` or `EXAMPLE:Feature:field:team`.
- Live required create fields that are not mapped are reported as drift for each issue type.
  This includes Description: when Jira requires it on a create screen, the drift check
  `EXAMPLE:Story:create_field:description` (or `EXAMPLE:Feature:...`) tells you that every create
  of that type must supply the top-level `description`, or that a Jira admin must make Description
  optional. Story and feature create still refuse, before any write, an input without
  `description` when Jira requires it.
- For each transition source status, `atl` needs a sample issue that Jira reports in that project
  and status. With `--issue KEY`, `atl` looks up that issue for every source status:
  - If the issue has that key, project, and source status, it is the sample for that source.
  - If the issue is accessible but its key, project, or status does not match, it is not used for
    that source. `atl` searches the project for an issue in that status instead.
  - If looking up the issue fails (for example `not_found`, `forbidden`, or a network error), the
    command fails with that error. It does not fall back to search.

  Without `--issue`, or after a mismatch, the most recently updated issue from the search is
  re-read to confirm its project and status. Each source status can therefore use a different
  sample. If neither `--issue` nor the search gives a matching issue, that source is `unverified`.
  `atl` then checks the live transition, `auto_set` placement and values, `pre_edit` edit
  eligibility, and required screen fields. Each transition check carries the `sample_issue` it
  used.
- If two creatable issue types share the configured story or feature type name, that type's create
  fields are reported as drift.

Each check is `pass`, `drift`, or `unverified`. A source status with no matching sample issue, or
truncated or malformed metadata (including transition screens without a `fields` map, or fields
without a boolean `required` flag), is `unverified`, never `pass`. Exit 0 means everything passed. Any drift is
`config_drift` (exit 4). Otherwise, any unverified check is `config_unverified` (exit 4).
