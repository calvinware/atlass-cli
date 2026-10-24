# `atl.yaml` cookbook

`atl.yaml` is your team's policy: which projects `atl` may touch, how friendly field names map to
Jira fields, what each transition needs, and who may create features. This page gives annotated
recipes. The exact rules for every key are in the
[configuration reference](configuration.md).

> [!NOTE]
> Every complete recipe below (the blocks that start with `version: 1`) is loaded by the test
> suite with `atl`'s real config parser, so they stay valid. Values such as URLs, project keys,
> field IDs, and statuses are placeholders.

## Workflow for admins

1. Start from [`atl.example.yaml`](../atl.example.yaml) and keep `atl.yaml` in the repository root,
   so every change goes through code review.
2. Fill in real URLs, project keys, field names or IDs, and workflow statuses.
3. Run `atl config check --issue KEY` against live Jira. Fix every `drift` and `unverified` check
   (see [Troubleshooting](troubleshooting.md)).
4. Re-run `atl config check` after any Jira screen, field, or workflow change.

> [!WARNING]
> Never put credentials in `atl.yaml`. `atl` never reads them from the file; people log in with
> `atl login`, which uses the OS keyring.

## Recipe 1: one project, the standard flow

Stories need acceptance criteria and a parent feature. Moving to In Progress sets a
`Work Started` option on the transition screen. Done needs a test results attachment.

```yaml
version: 1
jira:
  base_url: https://jira.example.test/jira     # https only; keep the context path
  projects:
    - key: EXAMPLE
      allowed_issue_types: [Story, Feature]
      story_type: Story
      feature_type: Feature
      feature_create_policy: human_confirmed   # agents need a human attestation
      fields:
        # Every project must map these two; story create fills them.
        acceptance_criteria: {jira_name: Acceptance Criteria, encoding: text}
        feature_link: {jira_name: Parent Link, encoding: issue_key_string}
        work_started: {jira_name: Work Started, encoding: single_option}
      create:
        story:
          required: [acceptance_criteria, feature_link]
      transitions:
        in_progress:
          from: [To Do]
          to: In Progress
          auto_set: {work_started: Yes}         # YAML 1.1 "Yes" stays the string "Yes"
        done:
          from: [In Progress]
          to: Done
          require:
            - kind: attachment_filename
              pattern: "{key}_TestResults_*"    # {key} becomes the issue key
confluence:
  base_url: https://confluence.example.test/confluence
```

## Recipe 2: disambiguate fields and require team fields on create

When two Jira fields share a display name, `jira_name` fails with the candidate IDs. Use `id`
instead. Here stories must also carry a team (single select) and components-style labels (multi
select), and features must name an owner.

```yaml
version: 1
jira:
  base_url: https://jira.example.test/jira
  projects:
    - key: PLAT
      allowed_issue_types: [Story, Epic]
      story_type: Story
      feature_type: Epic                       # your "feature" can be any issue type
      fields:
        acceptance_criteria: {id: customfield_10100, encoding: text}
        feature_link: {jira_name: Epic Link, encoding: issue_key_string}
        team: {id: customfield_10400, encoding: single_option}
        platforms: {id: customfield_10401, encoding: multi_option}
        owner: {jira_name: Feature Owner, encoding: text}
      create:
        story:
          required: [acceptance_criteria, feature_link, team]
        feature:
          required: [owner]
```

Callers supply the extra values in `fields`:

```json
{"project": "PLAT", "summary": "Cache tenant settings",
 "acceptance_criteria": "Given ... When ... Then ...", "feature_key": "PLAT-7",
 "fields": {"team": "Platform", "platforms": ["Linux", "Windows"]}}
```

## Recipe 3: set a resolution on Done, and an off-screen field with `pre_edit`

`resolution_option` matches the resolution by name. `qa_signoff` is not on the Done transition
screen, so the team opted in to `pre_edit`: `atl` edits the field first (PUT), re-checks, then
transitions (POST).

```yaml
version: 1
jira:
  base_url: https://jira.example.test/jira
  max_attachment_bytes: 20971520               # 20 MiB; Jira or a proxy may be lower
  projects:
    - key: EXAMPLE
      allowed_issue_types: [Story, Feature]
      story_type: Story
      feature_type: Feature
      fields:
        acceptance_criteria: {jira_name: Acceptance Criteria, encoding: text}
        feature_link: {jira_name: Parent Link, encoding: issue_key_string}
        resolution: {id: resolution, encoding: resolution_option}
        qa_signoff: {jira_name: QA Sign-off, encoding: single_option}
      transitions:
        done:
          from: [In Progress, In Review]
          to: Done
          pre_edit: true                       # strict boolean; opt-in only
          auto_set:
            resolution: Done                   # on the transition screen
            qa_signoff: Approved               # off-screen: set by a separate PUT
          require:
            - kind: attachment_filename
              pattern: "{key}_TestResults_*.xml"
```

> [!WARNING]
> `pre_edit` writes are **not atomic**. If the transition fails after the edit, the edit stays,
> the result is `partial_transition` (exit 7), and a retry repeats the edit. Prefer adding the
> field to the transition screen. See [`pre_edit` (non-atomic)](configuration.md#pre_edit-non-atomic).

## Recipe 4: two projects, automatic features in one

Stories in `APP` may link to features in `ROAD` because both projects are configured. `ROAD`
lets callers create features without a human attestation.

```yaml
version: 1
jira:
  base_url: https://jira.example.test/jira
  projects:
    - key: APP
      allowed_issue_types: [Story, Feature]
      story_type: Story
      feature_type: Feature
      fields:
        acceptance_criteria: {jira_name: Acceptance Criteria, encoding: text}
        feature_link: {jira_name: Parent Link, encoding: issue_key_string}
      transitions:
        start:
          from: [Backlog, Selected]
          to: In Progress
    - key: ROAD
      allowed_issue_types: [Story, Feature]
      story_type: Story
      feature_type: Feature
      feature_create_policy: automatic
      fields:
        acceptance_criteria: {jira_name: Acceptance Criteria, encoding: text}
        feature_link: {jira_name: Parent Link, encoding: issue_key_object}
```

A story's `feature_key` must be an issue of the configured `feature_type` in a configured project.
`feature find` searches every configured project's feature type.

## Excerpts

Internal CA and a reverse-proxy context path. The CA file path is relative to `atl.yaml` and
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
    pattern: "{key}_TestResults_*"        # EXAMPLE-42_TestResults_2026-10-24.xml
  - kind: attachment_filename
    pattern: "{key}_Coverage_[0-9]*.html" # EXAMPLE-42_Coverage_87.html
```

## Encodings

| Encoding | Use it for | Example value |
|----------|-----------|---------------|
| `text` | free-text string fields | `"Given ... When ... Then ..."` |
| `issue_key_string` | Parent Link, Epic Link, other key-as-string fields | `"EXAMPLE-10"` |
| `issue_key_object` | issue-link style fields that take `{"key": ...}` | `"EXAMPLE-10"` |
| `single_option` | single select (matched by option `value`) | `"Platform"` |
| `multi_option` | multi select (YAML list or JSON array) | `["Linux", "Windows"]` |
| `resolution_option` | the resolution field (matched by `name`) | `"Done"` |

If the live schema or allowed values cannot confirm a safe encoding, `atl` fails with a
field-specific fix instead of guessing.

## Drift

`atl config check` compares the file with live Jira and reports each check as `pass`, `drift`,
or `unverified`. Drift is [`config_drift`](reference/errors.md#config_drift) (exit 4); unverified
without drift is [`config_unverified`](reference/errors.md#config_unverified) (exit 4). Common
causes and fixes are in [Troubleshooting](troubleshooting.md#config-drift).
