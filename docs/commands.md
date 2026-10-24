# Command behavior

This page explains how commands behave: preflight, when checks run, transitions, attachments,
Confluence updates, and how to inspect writes before a retry. For every command's arguments,
input and output schemas, examples, and error codes, see the generated
[command reference](reference/commands.md) and [error catalog](reference/errors.md).

The JSON Schemas from `atl schema '<command>'` are the source of truth. All commands accept
`--config PATH`. Every command writes one JSON object to stdout and exits with one of the codes in
the [agent contract](../AGENTS.md#contract).

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
| `atl jira feature find --issue KEY \| --text TEXT [--limit N≤25]` | — | `candidates[]` (`key`, `project`, `summary`, `status`, `score`, `reasons`), `candidate_pool_limit`, `truncated`, `selection: "caller"` |
| `atl jira feature create --input PATH\|-` | `project`, `summary`, optional `description`, `fields`, `human_confirmation` | `key`, `policy`, `attestation` (`identity_verified: false`) |
| `atl jira attachment add KEY --file PATH` | file on disk | `attachments[]` |
| `atl jira comment add KEY --body-file PATH\|-` | UTF-8 text, max 1 MiB | `comment_id` |
| `atl jira search --jql JQL [--start N] [--limit N≤100]` | — | `issues[]`, `total`, `next_start` |

### Create preflight

Before the `POST /rest/api/2/issue`, `atl` checks all of the following. Any failure is
`requirements_unmet` (exit 4) with every check listed, and nothing is sent.

- Story create only: `acceptance_criteria` is present and not blank. The fix says to write it;
  atl never does.
- Story create only: `feature_key` is present, exists under that exact key, belongs to a project
  configured in `atl.yaml`, and is that project's `feature_type`. The fix points to
  `feature find`; atl never chooses.
- Feature create only: the project's `feature_create_policy` is satisfied. Under
  `human_confirmed`, a missing `human_confirmation` is `approval_required` (exit 4) before any
  request is made.
- Every field in the issue type's own configured list (`create.story.required` for story create,
  `create.feature.required` for feature create) has a non-blank value and is on that issue type's
  create screen. A listed field that is missing from the screen fails with a fix to add it to the
  screen or correct `atl.yaml`; it is never skipped. Story create does not read
  `create.feature.required`, and feature create does not read `create.story.required`.
- Every supplied field resolves to exactly one field on the live create screen (targeted,
  fully paginated `createmeta`) and serializes under its encoding.
- No two supplied friendly names resolve to the same Jira field ID.
- Every live required field without a default is supplied.

Truncated or malformed create metadata fails closed with `metadata_incomplete` (exit 6).

### When checks run

Passing preflight does not guarantee that Jira or Confluence will accept the write. Some checks
can only happen after a request is dispatched:

| When | Checks | Failure |
|------|--------|---------|
| Before the first write | Arguments and input JSON Schema; `atl.yaml`; credentials; create preflight (above); the transition checklist (`atl jira requirements`); attachment existence, regular-file type, and size limit (checked on the path and again on the opened file); Confluence `expected_version` against a fresh read | Input/config errors (exit 2), `requirements_unmet` or `approval_required` (exit 4), `version_conflict` (exit 5), `metadata_incomplete` (exit 6). Nothing is sent. |
| While an attachment streams | The file still has the size checked before upload | `outcome_unknown` (exit 6) with `details.dispatched: "aborted"`. The body is never completed, but the request may already have reached Jira, so inspect the issue view for a partial attachment before any retry. |
| After a `pre_edit` PUT | The issue and its transitions are re-read, and the full checklist runs again before the transition POST | `partial_transition` (exit 7). The edit has already been applied. |
| When the server handles a write | Server-side workflow validators, field validation, and server limits that REST metadata does not expose | `remote_validation`, `remote_limit_exceeded`, `not_found`, or `conflict` (exit 5). 401 is `auth_failed` and 403 is `forbidden` (exit 3): run `atl login` again, or ask an administrator for access to the project or space. 429 is `rate_limited` (exit 6). A 408, 499, or 5xx answer does not prove the write was refused, so it is `outcome_unknown` (exit 6). `partial_transition` (exit 7) if a `pre_edit` edit already succeeded. |
| After a 2xx write response | The response confirms the write: a non-empty issue id and a valid issue key in the target project for create; a non-empty comment id; the uploaded attachment name and size; for page create a valid page id, type `page`, the submitted title (leading and trailing whitespace may be trimmed), the submitted space (any letter case), and version 1; for page update the page id, title (same trimming rule), and next version. A write response over the 32 MiB cap is never parsed. | `outcome_unknown` (exit 6) with a command-specific inspection fix. Inspect before any retry. |

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
- The word search takes up to 100 of the most recently updated matches
  (`candidate_pool_limit`) and ranks them locally. `truncated: true` means more matches may
  exist: Jira either reported more than it returned, or filled the pool without reporting a
  total. An older match may be missing; use more specific `--text`. `--limit` only trims the
  ranked list and never sets `truncated`.
- Keys of configured projects named in `--text` are fetched by a separate query, so they are
  always ranked even when the word search is truncated.
- Score: +100 when a candidate's key appears in `--text`, +10 per shared summary word, and +5 for
  the same project as `--issue`.
- Ties are broken by project key, then issue number.

`--text "ABC-12"` is always treated as text. It cannot be confused with `--issue`, because the two
options are mutually exclusive.

### Attachments

Before upload, `atl` checks that the file exists, is a regular file, and is no larger than
`jira.max_attachment_bytes`. It opens the path without blocking (so a FIFO swapped in after the
check cannot hang the command), then checks the opened file's type and size and that it is the
same file (device and inode) that was checked. A path replaced between the check and the open,
even by a file of the same size, is `file_invalid` and nothing is sent. `atl` then copies the
opened file into a private temporary snapshot (owner-only; unnamed on POSIX, delete-on-close on
Windows; at most `jira.max_attachment_bytes`). If the file's size, modification time, or change
time differs after the copy, it is `file_invalid` and nothing is sent; wait until the file is fully
written and retry. The upload streams the snapshot, so rewriting the source during the upload does
not change the bytes sent. Change detection relies on file timestamps, so a same-size rewrite
within the file system's timestamp resolution while `atl` is copying can go unnoticed.

The upload is a streamed `multipart/form-data` with an exact `Content-Length` and
`X-Atlassian-Token: no-check`. As a last guard, if the stream yields fewer or more bytes than the
declared size, the upload is aborted before the body completes. Because the request may already
have reached Jira, this is `outcome_unknown` (exit 6) with `details.dispatched: "aborted"`; inspect
the issue view for a partial attachment before any retry.

The command succeeds only when Jira's 2xx response is a non-empty JSON list in which every item has
a non-empty string `id` and `filename` and a non-negative integer `size`, and at least one item has
the uploaded file's name and byte size. Any other 2xx response, including an empty or non-JSON
body, is `outcome_unknown` (exit 6). The fix names the file and size to look for in the issue view.

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
the read does not include `body.storage.value` as a string, a non-empty string `title`, or an
object `version` with a positive integer `number`, or returns a different content ID, `read` and
`update` fail with `response_invalid` (exit 6) and nothing is written. (`update` with a new
`title` does not need the current title.) `update` also
refuses content whose type is not `page` (for example a blog post) with `input_invalid` (exit 2)
before writing. An empty body is kept only when Confluence
returns one explicitly. A
409 from a concurrent edit is also reported as `version_conflict`, with a re-read-and-merge fix.

The update succeeds only when Confluence's 2xx response contains the requested page `id`, the
submitted `title`, and an integer `version.number` equal to `expected_version + 1`. Any other 2xx
response, including an empty or non-JSON body, is `outcome_unknown` (exit 6) with a fix to run
`atl confluence page read ID` and compare before retrying. `atl` never fills in a missing title or
version.

## Writes and retries

No write is ever retried. A write that was dispatched but whose result is unknown (timeout,
disconnect, HTTP 408 or 499, 5xx, redirect, an attachment file that changed during upload, unparseable or oversized success, or a success response that does not
confirm the write) returns `outcome_unknown` (exit 6). Inspect
remote state before running it again. `atl jira search` returns status and summary only. It
cannot show comments, attachments, or custom field values, so use the method for each write:

| Uncertain write | How to inspect |
|-----------------|----------------|
| `jira story create`, `jira feature create` | `atl jira search --jql 'project = KEY AND summary ~ "<words>" AND created >= -1h ORDER BY created DESC'` (narrow the window to when you ran the command). Open each candidate in the Jira issue view to confirm it is yours. |
| `jira comment add` | Open the issue in the Jira issue view and look for a comment whose body exactly matches what you sent. |
| `jira attachment add` | Open the issue in the Jira issue view and look for the exact filename and size. `atl jira requirements KEY --to STATUS` lists attachment names when an attachment rule fails. |
| `jira transition` | `atl jira requirements KEY --to STATUS` (`from_status`) or `atl jira search --jql 'key = KEY'` (`status`). For a `pre_edit` edit, check the edited field values in the issue view. |
| `confluence page create` | Run the searches in `error.fix` (`details.recovery_argv`): first `space = "KEY" AND type = page AND (title = "<submitted>" OR title = "<trimmed>")`, then, if that finds nothing, the space's newest pages (`space = "KEY" AND type = page ORDER BY created DESC`), following `next_start` through every result page. Read each plausible candidate with `atl confluence page read ID` and compare `title`, `space_key`, `ancestors`, and `body_storage` with what you sent. An empty exact-title search alone does not show the page was not created. Results can shift between pages, so a scan with no match does not establish absence either: ask a human to confirm the page does not exist before any retry. |
| `confluence page update` | `atl confluence page read ID` and compare `version`, `title`, and `body_storage` with what you sent. |

Recovery commands inside `error.fix` are quoted for POSIX shells (sh, bash, zsh) only. In
`cmd.exe`, PowerShell, or a program, do not paste them; pass each list in `details.recovery_argv`
as separate arguments instead (`outcome_unknown` from a transition or Confluence page create, and
`partial_transition`).

Agents inspect only through `atl` and, where the table says issue view, by asking a human to
check it. Agents never call the REST APIs directly. As a human-only exception, a person may use an
approved read-only REST call (for example `GET /rest/api/2/issue/KEY/comment`) in place of the
issue view.

Retry only when inspection shows the write was **not** applied. If it was applied, do not retry:
continue from the existing item. If you cannot tell, or more than one candidate matches (for
example two issues with the same summary), stop and ask a human to resolve it, including deleting
any duplicate by hand. A search whose results changed or were incomplete while you read them
cannot show that a write was not applied. If the outcome is still uncertain, a human must resolve
it before any retry. For `partial_transition` (exit 7) and `pre_edit` retries, also follow
[`pre_edit` (non-atomic)](configuration.md#pre_edit-non-atomic): reconcile current field values
and get a fresh `ready: true` from `atl jira requirements` first.
