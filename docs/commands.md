# Command reference

The JSON Schemas from `atl schema '<command>'` are the source of truth. This page summarizes them.
All commands accept `--config PATH`. Every command writes one JSON object to stdout and exits with
one of the codes in the [agent guide](../AGENTS.md#contract).

## Discovery

| Command | Notes |
|---------|-------|
| `atl commands` | Lists every command, its summary, and the exit code table. Offline. |
| `atl schema '<path>'` | Returns `arguments`, `input`, `success`, `error`, `error_codes`, and `exit_codes` for one command (`schema_version: 1`). Unquoted words also work: `atl schema jira story create`. Offline. |
| `atl ... --help` | JSON help for any command or group. |

## Authentication

```
atl login --service jira|confluence|all [--auth pat|basic] [--username NAME]
          [--token-stdin | --password-stdin]
```

- The default `--auth` is `pat`, which sends `Authorization: Bearer`. `basic` sends HTTP Basic
  with `--username`.
- Secrets are read from stdin (`--token-stdin` / `--password-stdin`, one line per service) or from
  a hidden TTY prompt. A secret is never accepted as an argument.
- Prompts go to stderr as JSON, for example
  `{"prompt": {"kind": "token", "service": "jira", "message": "..."}}`. Only the result goes to
  stdout. Without a TTY, a stdin flag is required.
- The credential is checked first against `GET /rest/api/2/myself` (Jira) or
  `GET /rest/api/user/current` (Confluence; an anonymous user is refused). It is stored only if
  the check passes.
- The OS keyring entry is keyed by service and normalized base URL. Only native backends are
  allowed: macOS Keychain, Windows Credential Manager, or Linux Secret Service/KWallet. There is no
  plaintext fallback (`keyring_unavailable`, exit 3).
- With `--service all`, stdin lines are read in the order Jira, then Confluence. Each service
  reports `stored` or `failed`. Any failure is `login_failed` (exit 3) with per-service details.

## Config

`atl config check [--issue KEY]` is a read-only drift report. See
[configuration](configuration.md#drift-checks).

## Jira

| Command | Input | Result |
|---------|-------|--------|
| `atl jira story create --input PATH\|-` | `project`, `summary`, `acceptance_criteria`, `feature_key`, optional `description`, `fields` (friendly names) | `key`, `id`, `url`, `checks` |
| `atl jira requirements KEY --to STATUS` | — | checklist: `ready`, `checks[]`, `plan.operations[]`, `plan.atomic`, `notice` |
| `atl jira transition KEY --to STATUS` | — | `operations[]` with statuses, `atomic`, `transition_id` |
| `atl jira feature find --issue KEY \| --text TEXT [--limit N≤25]` | — | `candidates[]` (`key`, `project`, `summary`, `status`, `score`, `reasons`), `selection: "caller"` |
| `atl jira feature create --input PATH\|-` | `project`, `summary`, optional `description`, `fields`, `human_confirmation` | `key`, `policy`, `attestation` (`identity_verified: false`) |
| `atl jira attachment add KEY --file PATH` | file on disk | `attachments[]` |
| `atl jira comment add KEY --body-file PATH\|-` | UTF-8 text, max 1 MiB | `comment_id` |
| `atl jira search --jql JQL [--start N] [--limit N≤100]` | — | `issues[]`, `total`, `next_start` |

### Create preflight

Before the `POST /rest/api/2/issue`, `atl` checks all of the following. Any failure is
`requirements_unmet` (exit 4) with every check listed, and nothing is sent.

- `acceptance_criteria` is present and not blank. The fix says to write it; atl never does.
- `feature_key` is present, exists, and is the project's `feature_type`. The fix points to
  `feature find`; atl never chooses.
- Every supplied field resolves to exactly one field on the live create screen (targeted,
  fully paginated `createmeta`) and serializes under its encoding.
- Every `create.required` field has a value.
- No two supplied friendly names resolve to the same Jira field ID.
- Every live required field without a default is supplied.

Truncated or malformed create metadata fails closed with `metadata_incomplete` (exit 6).
`create.required` applies fully to story create. Feature create enforces each `create.required`
field that is on the feature create screen, and also checks the live create screen and the
project's `feature_create_policy`.

### Requirements and transitions

`requirements` and `transition` choose the `atl.yaml` project from the key you pass. If Jira
returns the issue under a different key or project (for example, it was moved), the command fails
with `issue_key_mismatch` (exit 2) and writes nothing. Re-run with the current key; its project
must be configured. After a `pre_edit` edit, the same check runs on the refreshed issue, and a
mismatch there is reported as `partial_transition` (exit 7).

Transition metadata must include each transition's `fields` map (requested with
`expand=transitions.fields`). A missing, null, or malformed map, or a field without a boolean
`required` flag, fails with `metadata_incomplete` (exit 6) instead of being treated as an empty
screen.

### Feature ranking

Ranking is deterministic:

- Search is limited to each configured project's `feature_type`.
- Words of 3 or more characters (stop words removed, at most 10) are matched with
  `summary ~ "word"`. All JQL literals are escaped.
- Score: +100 when a candidate's key appears in `--text`, +10 per shared summary word, and +5 for
  the same project as `--issue`.
- Ties are broken by project key, then issue number.

`--text "ABC-12"` is always treated as text. It cannot be confused with `--issue`, because the two
options are mutually exclusive.

### Attachments

Before upload, `atl` checks that the file exists, is a regular file, and is no larger than
`jira.max_attachment_bytes`. The upload is a streamed `multipart/form-data` with an exact
`Content-Length` and `X-Atlassian-Token: no-check`. If the file shrinks or grows during upload,
the upload is aborted before the body completes (`file_invalid`, `details.dispatched: "aborted"`).

## Confluence

All requests use Data Center `/rest/api/content` under the configured context path.

| Command | Input | Result |
|---------|-------|--------|
| `atl confluence page read ID` | — | `title`, `space_key`, `version`, `ancestors`, `body_storage`, `content_trust: "untrusted"` |
| `atl confluence page search --cql CQL [--start N] [--limit N≤100]` | — | `results[]`, `next_start` |
| `atl confluence page create --input PATH\|-` | `space_key`, `title`, `body_storage` (storage format), optional `parent_id` | `id`, `version` |
| `atl confluence page update ID --input PATH\|-` | `expected_version` and at least one of `title`, `body_storage` | `version`, `previous_version` |

`update` reads the page first. If the current version differs from `expected_version`, it fails
with `version_conflict` (exit 5) and nothing is written. Otherwise it sends version
`expected_version + 1`, keeping the current title or body for whichever field was not supplied. If
the read does not include `body.storage.value` as a string, `read` and `update` fail with
`response_invalid` (exit 6) and nothing is written. An empty body is kept only when Confluence
returns one explicitly. A
409 from a concurrent edit is also reported as `version_conflict`, with a re-read-and-merge fix.

## Writes and retries

No write is ever retried. A write that was dispatched but whose result is unknown (timeout,
disconnect, 5xx, redirect, or unparseable success) returns `outcome_unknown` (exit 6). Inspect
remote state before running it again. `atl jira search` returns status and summary only. It
cannot show comments, attachments, or custom field values, so use the method for each write:

| Uncertain write | How to inspect |
|-----------------|----------------|
| `jira story create`, `jira feature create` | `atl jira search --jql 'project = KEY AND summary ~ "<words>" AND created >= -1h ORDER BY created DESC'` (narrow the window to when you ran the command). Open each candidate in the Jira issue view to confirm it is yours. |
| `jira comment add` | Open the issue in the Jira issue view and look for a comment whose body exactly matches what you sent. |
| `jira attachment add` | Open the issue in the Jira issue view and look for the exact filename. `atl jira requirements KEY --to STATUS` lists attachment names when an attachment rule fails. |
| `jira transition` | `atl jira requirements KEY --to STATUS` (`from_status`) or `atl jira search --jql 'key = KEY'` (`status`). For a `pre_edit` edit, check the edited field values in the issue view. |
| `confluence page create` | `atl confluence page search --cql 'space = "KEY" AND title = "<title>"'`. |
| `confluence page update` | `atl confluence page read ID` and compare `version`, `title`, and `body_storage` with what you sent. |

A human may also inspect with an approved read-only REST call (for example
`GET /rest/api/2/issue/KEY/comment`) in place of the issue view.

Retry only when inspection shows the write was **not** applied. If it was applied, do not retry:
continue from the existing item. If you cannot tell, or more than one candidate matches (for
example two issues with the same summary), stop and ask a human to resolve it, including deleting
any duplicate by hand. For `partial_transition` (exit 7) and `pre_edit` retries, also follow
[`pre_edit` (non-atomic)](configuration.md#pre_edit-non-atomic): reconcile current field values
and get a fresh `ready: true` from `atl jira requirements` first.
