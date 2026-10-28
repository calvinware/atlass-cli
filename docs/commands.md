# Command behavior

This page explains how commands behave: output modes, the preflight inside every write, how names
are resolved, transitions, attachments, Confluence updates, and how to inspect a write whose
outcome is unknown. For every command's arguments, input and output schemas, examples, and error
codes, see the generated [command reference](reference/commands.md) and
[error catalog](reference/errors.md).

The JSON Schemas from `yojo schema '<command>'` are the source of truth. Every command writes one
JSON object to stdout in JSON mode and exits with one of the codes in the
[agent contract](../AGENTS.md#contract).

## Output: one envelope, two renderings

| Mode | When | stdout |
|------|------|--------|
| JSON | `--json`, or stdout is not a terminal (piped, redirected, captured by a program) | exactly one compact JSON object on one line, ASCII-escaped, no color |
| Terminal | stdout is a terminal and `--json` is not given | the same envelope rendered for people: aligned tables, plain-language status, the next step, a link after a write |

`yojo setup` also answers in JSON when stdin is not a terminal or `TERM=dumb`, because the setup
app cannot run there: one `usage_error` envelope (exit 2) listing the step-by-step commands.

The exit code is the same in both modes. `NO_COLOR` (any value) or `TERM=dumb` turns color off. In
a terminal, a write first prints `Sending: ...` to stderr, naming what will change, just before the
request goes out. A refused write says plainly that nothing was sent. On first run with no
`yojo.yaml`, bare `yojo` opens setup in a usable terminal; with JSON or piped output it returns
a `usage_error` envelope (exit 2). With a config already present, bare `yojo` returns usage.

## Global options

| Option | Meaning |
|--------|---------|
| `--json` | Print the JSON envelope even on a terminal. Agents and scripts should always pass it. |
| `--base-url URL` | The Jira, Confluence, or Bitbucket HTTPS root for this call. Works with no `yojo.yaml`. If a `yojo.yaml` sets a different URL for that service, the call is refused with `input_invalid` and nothing is sent. |
| `--config PATH` | Use this `yojo.yaml` instead of searching from the current directory upward. The file must exist. |

## Discovery

| Command | Notes |
|---------|-------|
| `yojo commands` | Lists every command with its summary, usage line, and example, plus the global options and exit code table. Offline. |
| `yojo schema '<path>'` | Returns `usage`, `example`, `arguments`, `input`, `success`, `error`, `error_codes`, and `exit_codes` for one command (`schema_version: 1`). Unquoted words also work: `yojo schema jira issue create`. Offline. |
| `yojo ... --help` | Help for any command or group (JSON in JSON mode). |

## Authentication

```
yojo login --service jira|confluence|bitbucket|all [--base-url URL] [--auth pat|basic]
           [--username NAME] [--token-stdin | --password-stdin]
```

- The URL comes from `--base-url` or from `yojo.yaml`. `--service all` means Jira and Confluence
  and uses the URLs in `yojo.yaml`; with `--base-url`, log in to one service at a time. Log in to
  Bitbucket on its own with `--service bitbucket`.
- The default `--auth` is `pat`, which sends `Authorization: Bearer`. `basic` sends HTTP Basic
  with `--username`.
- Secrets are read from stdin (`--token-stdin` / `--password-stdin`, one line per service) or from
  a hidden terminal prompt. A secret is never accepted as an argument, from `yojo.yaml`, or from the
  environment.
- In JSON mode, prompts go to stderr as JSON, for example
  `{"prompt": {"kind": "token", "service": "jira", "message": "..."}}`. Only the result goes to
  stdout. Without a terminal, a stdin flag is required.
- The credential is checked first against `GET /rest/api/2/myself` (Jira) or
  `GET /rest/api/user/current` (Confluence; an anonymous user is refused). For Bitbucket, two
  checks must pass: `GET /rest/api/1.0/application-properties` must identify Bitbucket (this
  endpoint answers without a login, so it cannot prove the token on its own), and
  `GET /rest/api/1.0/profile/recent/repos?limit=1`, which answers only for a signed-in user, must
  accept the token. Bitbucket does not report a username there, so `user` is `null` and `version`
  shows the Bitbucket version. Bitbucket takes a token only; `--auth basic` is refused. The
  credential is stored only if every check passes.
- The OS keyring entry (service name `yojo`) is keyed by service and normalized base URL, so the
  same login works with or without `yojo.yaml` as long as the URL matches. Only native backends
  are allowed: macOS Keychain, Windows Credential Manager, or Linux Secret Service/KWallet. There
  is no plaintext fallback (`keyring_unavailable`, exit 3).
- With `--service all`, stdin lines are read in the order Jira, then Confluence. Each service
  reports `stored` or `failed`. Any failure is `login_failed` (exit 3) with per-service details.

> [!NOTE]
> Credentials stored by `atl` (keyring service `atlass-cli`) are not read. Run `yojo login` once per
> instance.

## Config

`yojo config check` is a read-only drift report for `yojo.yaml`. It needs a config file. It checks
Jira when a `jira` section is present, and when a `bitbucket` section is present it checks that the
URL answers as Bitbucket Data Center and that the stored token is accepted. A Bitbucket-only file
works. A Confluence-only file reports that it has no team rules to check. Mixed files collect
independent Jira and Bitbucket results. `--base-url` applies to Jira when Jira is configured,
otherwise to Bitbucket. See
[configuration](configuration.md#drift-checks).

## Jira

| Command | Main inputs | Result |
|---------|-------------|--------|
| `yojo jira issue create PROJECT --type TYPE --summary TEXT` | `--description`, repeatable `--set NAME=VALUE`, or `--input -` | `key`, `url`, `status`, `issue_type`, `project`, `changed[]` |
| `yojo jira issue get KEY` | `--fields 'NAME,NAME'`, `--full` | `key`, `url`, `summary`, `status`, `issue_type`, `project`, `assignee`, `updated`, `fields[]`, `more`, `content_trust` |
| `yojo jira issue update KEY` | `--summary`, `--description`, repeatable `--set`, or `--input -` | `key`, `url`, `status`, `changed[]` |
| `yojo jira issue link KEY OTHER --type TYPE` | link name or either direction's wording | `key`, `url`, `relation`, `other`, `link_type` |
| `yojo jira requirements KEY --to STATUS` | optional `--set` | checklist: `ready`, `checks[]`, `plan.operations[]`, `plan.atomic`, `notice` |
| `yojo jira transition KEY --to STATUS` | optional `--set`, or `--input -` | `url`, `from_status`, `status`, `changed[]`, `atomic`, `operations[]` |
| `yojo jira attachment add KEY --file PATH` | file on disk | `url`, `attachments[]` |
| `yojo jira comment add KEY --body TEXT` | or `--body-file PATH\|-` (UTF-8 text, max 1 MiB) | `url`, `comment_id` |
| `yojo jira search --jql JQL [--limit N≤100] [--start N]` | default limit 20 | `issues[]`, `total`, `next_start`, `content_trust` |

### Names and values

- **Fields** are named the way Jira shows them (`Story Points`), by a `yojo.yaml` alias
  (`points`), or by raw ID (`customfield_10002`), in any letter case. A name that matches two
  fields fails with both IDs listed, unless exactly one matches with the same letter case. A raw
  ID always works.
- **Options, priorities, resolutions, versions, and components** are matched by the label shown
  in Jira. Disabled options are not allowed. Multi-value fields take a comma-separated list (or a
  JSON array in `--input`).
- **People** are matched by username, display name, or email with one bounded user search. Only an
  exact match is used; a single near match is offered as a suggestion.
- **Statuses** for `--to` match the destination status or the transition name, in any case.
- A near miss fails with "Did you mean ...?". The suggestion goes into `details.retry`; it is never
  applied silently.
- Field types `yojo` cannot set safely (for example cascading selects, or a select whose allowed
  values Jira does not list) fail with a fix instead of a guess.

`--set NAME=VALUE` is split at the first `=`. Giving the same field twice with different values
(for example once with `--set` and once in `--input`) is `input_invalid` before anything is sent.

### Preflight inside every write

Before the first write request, each Jira write checks everything it can, and reports **every**
problem in one response. Any failed or unverified check is `requirements_unmet` (exit 4) with
`details.nothing_sent: true`, `details.checks`, and `details.retry`; nothing is sent.

| Command | Checks |
|---------|--------|
| `issue create` | The summary is present. The issue type exists in the project (the project must be visible to you). Every `--set` field is on that type's live create screen (fully paginated `createmeta`) and its value is valid. No two names point at the same field, and `--set` does not repeat a field that has its own flag (project, type, summary). Every field Jira marks required without a default is supplied. Every field in `create.<Type>.required` in `yojo.yaml` is supplied and on the screen. |
| `issue update` | Every field is on the issue's edit screen (`editmeta`), allows `set`, and its value is valid. |
| `issue link` | Both issues exist and are visible. The link type matches. You hold `LINK_ISSUES`. The same link does not already exist. |
| `transition` | See [Requirements and transitions](#requirements-and-transitions). |
| `comment add` | The body is not blank. The issue exists. You hold `ADD_COMMENTS`. |
| `attachment add` | The local file checks (below). The issue exists. You hold `CREATE_ATTACHMENTS`. Attachments are enabled and the file fits Jira's reported upload limit. |

A permission or setting that Jira does not report is `unverified`, which also refuses the write.
Truncated or malformed metadata fails closed with `metadata_incomplete` (exit 6).

`details.retry` is the full corrected command line, quoted for POSIX shells, and
`details.retry_argv` is the same command as an argument list. It keeps your arguments and applies
each fix it can: a suggested name, a dropped field that cannot be set, or a `<placeholder>` where
content is missing. `retry` is `null` when the value to fix came from `--input`; edit the input
document instead.

### When checks run

Passing preflight does not guarantee that Jira or Confluence will accept the write. Some checks
can only happen after a request is dispatched:

| When | Checks | Failure |
|------|--------|---------|
| Before the first write | Arguments and input JSON Schema; `yojo.yaml`; credentials; the preflight above; attachment existence, regular-file type, and size limit (checked on the path and again on the opened file); Confluence `expected_version` against a fresh read | Input/config errors (exit 2), `requirements_unmet` (exit 4), `version_conflict` (exit 5), `metadata_incomplete` (exit 6). Nothing is sent. |
| While an attachment streams | The file still has the size checked before upload | `outcome_unknown` (exit 6) with `details.dispatched: "aborted"`. The body is never completed, but the request may already have reached Jira, so inspect the issue view for a partial attachment. |
| After a `pre_edit` PUT | The issue and its transitions are re-read, and the full checklist runs again before the transition POST | `partial_transition` (exit 7). The edit has already been applied. |
| When the server handles a write | Server-side workflow validators, field validation, and server limits that REST metadata does not expose | `remote_validation`, `remote_limit_exceeded`, `not_found`, or `conflict` (exit 5). 401 is `auth_failed` and 403 is `forbidden` (exit 3): run `yojo login` again, or ask an administrator for access to the project or space. 429 is `rate_limited` (exit 6). A 408, 499, or 5xx answer does not prove the write was refused, so it is `outcome_unknown` (exit 6). `partial_transition` (exit 7) if a `pre_edit` edit already succeeded. |
| After a 2xx write response | The response confirms the write: a non-empty issue id and a valid issue key in the target project for create; the values Jira reports after an update (read back); the link on the source issue (read back); a non-empty comment id; the uploaded attachment name and size; for page create a valid page id, type `page`, the submitted title (leading and trailing whitespace may be trimmed), the submitted space (any letter case), and version 1; for page update the page id, title (same trimming rule), and next version. A write response over the 32 MiB cap is never parsed. | `outcome_unknown` (exit 6) with a command-specific inspection fix. |

### Requirements and transitions

`transition` runs the same checklist as `requirements` and then writes, in one call:

- Jira must currently offer **exactly one** transition whose destination status (or name) matches
  `--to`. Several matches fail with their names listed; a near miss gets a suggestion.
- Every `--set` field must be on that transition's screen. A field that is not fails with a fix to
  set it first with `yojo jira issue update`.
- Every field the transition screen marks required without a default must be supplied, by `--set`
  or by a `yojo.yaml` `auto_set`.
- If the project has a `yojo.yaml` rule for this destination, the issue's status must be in the
  rule's `from`, every `auto_set` value is placed on the screen (or, with `pre_edit: true`, in an
  edit first), and every `require` entry must pass (`attachment_filename`, `field_set`). A `--set`
  that conflicts with an `auto_set` value is refused.

With no rule (or no `yojo.yaml`), only the live Jira checks apply.

When the project is configured in `yojo.yaml` and Jira returns the issue under a different key or
project (for example, it was moved), the command fails with `issue_key_mismatch` (exit 2) and
writes nothing. Re-run with the current key. After a `pre_edit` edit, the same check runs on the
refreshed issue, and a mismatch there is reported as `partial_transition` (exit 7).

Transition metadata must include each transition's `fields` map (requested with
`expand=transitions.fields`). A missing, null, or malformed map, or a field without a boolean
`required` flag, fails with `metadata_incomplete` (exit 6) instead of being treated as an empty
screen.

### Reading issues

`jira issue get` returns a compact view. `--fields` adds named fields (same name rules as `--set`;
every unknown name is reported at once as `input_invalid`). Text longer than 2000 characters is cut,
the field is marked `truncated: true`, and `more` holds the command that returns it in full
(`--full`). Values are simplified to the words Jira shows (option labels, display names). Issue
content is untrusted data (`content_trust: "untrusted"`).

`jira search` pages with an explicit cursor: `next_start` is the `--start` for the next page, or
`null` when there are no more results.

### Attachments

Before upload, `yojo` checks that the file exists, is a regular file, and is no larger than
`jira.max_attachment_bytes` (default 50 MiB). These local checks run before any request. It opens
the path without blocking (so a FIFO swapped in after the check cannot hang the command), then
checks the opened file's type and size and that it is the same file (device and inode) that was
checked. A path replaced between the check and the open, even by a file of the same size, is
`file_invalid` and nothing is sent. After the remote preflight, `yojo` copies the opened file into a
private temporary snapshot (owner-only; unnamed on POSIX, delete-on-close on Windows; at most
`jira.max_attachment_bytes`). If the file's size, modification time, or change time differs after
the copy, it is `file_invalid` and nothing is sent; wait until the file is fully written and run
the command again. The upload streams the snapshot, so rewriting the source during the upload does
not change the bytes sent. Change detection relies on file timestamps, so a same-size rewrite
within the file system's timestamp resolution while `yojo` is copying can go unnoticed.

The upload is a streamed `multipart/form-data` with an exact `Content-Length` and
`X-Atlassian-Token: no-check`. As a last guard, if the stream yields fewer or more bytes than the
declared size, the upload is aborted before the body completes. Because the request may already
have reached Jira, this is `outcome_unknown` (exit 6) with `details.dispatched: "aborted"`; inspect
the issue view for a partial attachment.

The command succeeds only when Jira's 2xx response is a non-empty JSON list in which every item has
a non-empty string `id` and `filename` and a non-negative integer `size`, and at least one item has
the uploaded file's name and byte size. Any other 2xx response, including an empty or non-JSON
body, is `outcome_unknown` (exit 6). The fix names the file and size to look for in the issue view.

## Confluence

All requests use Data Center `/rest/api/content` under the configured context path.

| Command | Input | Result |
|---------|-------|--------|
| `yojo confluence page read ID [--full]` | — | `title`, `space_key`, `version`, `url`, `ancestors` (nearest 5), `body_storage`, `body_chars`, `truncated`, `more`, `content_trust: "untrusted"` |
| `yojo confluence page search --cql CQL [--start N] [--limit N≤100]` | default limit 20 | `results[]`, `next_start`, `content_trust` |
| `yojo confluence page create --input PATH\|-` | `space_key`, `title`, `body_storage` (storage format), optional `parent_id` | `id`, `version`, `url` |
| `yojo confluence page update ID --input PATH\|-` | `expected_version` and at least one of `title`, `body_storage` | `version`, `previous_version`, `url` |

`page read` returns at most 2000 characters of the body unless `--full` is given; `truncated` and
`more` say how to get the rest, and `body_chars` is the full length.

`update` reads the page first. If the current version differs from `expected_version`, it fails
with `version_conflict` (exit 5) and nothing is written. Otherwise it sends version
`expected_version + 1`, keeping the current title or body for whichever field was not supplied. If
the read does not include `body.storage.value` as a string, a non-empty string `title`, or an
object `version` with a positive integer `number`, or returns a different content ID, `read` and
`update` fail with `response_invalid` (exit 6) and nothing is written. (`update` with a new
`title` does not need the current title.) `update` also refuses content whose type is not `page`
(for example a blog post) with `input_invalid` (exit 2) before writing. An empty body is kept only
when Confluence returns one explicitly. A 409 from a concurrent edit is also reported as
`version_conflict`, with a re-read-and-merge fix.

The update succeeds only when Confluence's 2xx response contains the requested page `id`, the
submitted `title`, and an integer `version.number` equal to `expected_version + 1`. Any other 2xx
response, including an empty or non-JSON body, is `outcome_unknown` (exit 6) with a fix to run
`yojo confluence page read ID --full` and compare. `yojo` never fills in a missing title or version.

## Bitbucket

Bitbucket Data Center only, through REST 1.0 under the configured context path. A repository is
always `PROJECT/slug`, in arguments and in results. See the
[Bitbucket guide](guides/bitbucket.md) for tasks.

| Command | Main inputs | Result |
|---------|-------------|--------|
| `yojo bitbucket repo list [--project PROJECT]` | `--start`, `--limit N≤100` (default 20) | `repos[]` (`repo`, `name`, `url`), `next_start` |
| `yojo bitbucket repo get PROJECT/slug` | — | `name`, `state`, `public`, `default_branch`, `clone_http`, `clone_ssh`, `url` |
| `yojo bitbucket branch list --repo PROJECT/slug` | `--filter TEXT`, `--start`, `--limit` | `branches[]` (`name`, `default`, `commit`), `next_start` |
| `yojo bitbucket pr list --repo PROJECT/slug` | `--state OPEN\|MERGED\|DECLINED\|ALL` (any case, default OPEN), `--start`, `--limit` | `pull_requests[]` (`id`, `title`, `state`, `source`, `target`, `author`, `updated`), `next_start` |
| `yojo bitbucket pr get ID --repo PROJECT/slug` | `--full` | `title`, `state`, `source`, `target`, `author`, `reviewers[]`, `description`, `truncated`, `more`, `url` |
| `yojo bitbucket pr diff ID --repo PROJECT/slug` | `--path FILE`, `--old-path FILE` (with `--path`, for a renamed file), `--full` | `files[]`, `diff` (unified), `truncated`, `server_truncated`, `truncated_files`, `complete`, `more`, `inspect`, `url` |
| `yojo bitbucket pr create --repo PROJECT/slug --source BRANCH --target BRANCH --title TEXT` | `--description`, or `--input -` | `id`, `title`, `source`, `target`, `state`, `url` |
| `yojo bitbucket pr comment ID --repo PROJECT/slug --body TEXT` | or `--body-file PATH\|-` (UTF-8 text, max 1 MiB) | `comment_id`, `url` |

Every list returns Bitbucket's own `nextPageStart` as `next_start`; `yojo` never computes it as
`start + limit`, because Bitbucket may skip items you cannot see. A page that is not the last but
has a missing, repeating, or backward cursor, more items than asked for, or a different `start`
fails with `response_invalid` (exit 6). Pull request titles in a list are shortened to keep the page
small; `pr get` shows the full title.

`pr diff` uses Bitbucket's structured diff and renders a unified diff. Without `--full` it keeps
about 2000 characters, cut at a line end, and the first 10 changed files. `truncated` says `yojo`
cut it; `server_truncated` and `truncated_files` say Bitbucket did. `complete` is `true` only when
neither happened. `more`, when set, is a command that may show more: `--full`, or
`--path FILE --full` (plus `--old-path OLD` for a renamed file) for the first file Bitbucket cut.
A file can still be cut when fetched alone, and a cut of the whole diff has no `yojo` continuation,
so `more` can be `null` while `complete` is `false`. Then `inspect` is a `git diff FROM TO -- FILE`
command, using the two commits Bitbucket compared, for a clone that has both; the pull request's
diff view (`url`) shows the same change. With `--path`, a response for a different file fails with
`response_invalid`, and a file the pull request does not change is `not_found`. Header lines quote
a file name containing a newline or control character the way Git does. In a source line, any
other character that starts a new line for some readers (vertical tab, form feed, `\x1c`-`\x1e`,
`\x85`, U+2028, U+2029) is shown escaped (for example `\x0c` or `\u2028`), so `diff` splits into
lines only at `\n`; a source line containing `\n` or `\r`, or a file, hunk, or segment whose
hunks, segments, or lines are not lists, fails with `response_invalid`. If a diff is too large to
fetch at all, the error names the `--path` form.

`pr create` and `pr comment` check everything first and send nothing when a check fails or cannot
be completed:

- `pr create`: a non-blank title of at most 255 characters; the repository is visible to you; the
  source and target branches each resolve to exactly one branch (exact case first, then a unique
  any-case match; a near miss is suggested and used in `retry`); the branches differ; and no open
  pull request already goes from that source to that target. A branch whose ID is not
  `refs/heads/` plus its name is not used. The duplicate check reads every page of open pull
  requests from the source branch, up to 1000; if it cannot finish, or an entry lacks either
  branch or repository, the check is `unverified` and nothing is sent. A 403, timeout, or service
  error during a preflight read also becomes an `unverified` check alongside independent local
  failures. Repository visibility proves read access only; it cannot prove that the token has
  permission for pull request actions. Bitbucket may still reject the POST. Values from `--input`
  get no command-line `retry`.
- `pr comment`: a non-blank comment, the pull request exists, and it has no comment with exactly
  the same text (scanning up to 1000 activity entries; otherwise, or when an entry has no
  action or a comment entry has no text, `unverified`).

A create succeeds only when Bitbucket's response has a pull request ID, the requested source and
target branches in the requested repository, the submitted title, and, when a description was
sent, exactly that description (an empty one may come back missing). A comment succeeds only when
the response has a comment ID and the submitted text. A rejection (for example 409 when Bitbucket
already has an open pull request for the pair) shows Bitbucket's `errors[].message` in
`details.messages`, or in `details.fields` under the field it names. Approving, merging, and
declining pull requests are left to people; there are no commands for them.

## Writes and retries

No write is ever retried. A write that was dispatched but whose result is unknown (timeout,
disconnect, HTTP 408 or 499, 5xx, redirect, an attachment file that changed during upload,
unparseable or oversized success, or a success response or read-back that does not confirm the
write) returns `outcome_unknown` (exit 6) with no `retry`. Inspect remote state before running it
again, using the method for each write:

| Uncertain write | How to inspect |
|-----------------|----------------|
| `jira issue create` | Run the search in `error.fix`: `yojo jira search --jql 'project = KEY AND created >= -1h ORDER BY created DESC'` (widen the window if needed) and look for your summary. Open each candidate with `yojo jira issue get` to confirm it is yours. |
| `jira issue update` | `yojo jira issue get KEY --fields '<the fields you set>'` and compare the values with what you sent. A workflow or automation may have changed them. |
| `jira issue link` | `yojo jira issue get KEY --fields issuelinks` on the source issue named in `error.fix`. |
| `jira comment add` | Open the issue link in `error.fix` in the Jira issue view and look for a comment whose body exactly matches what you sent. |
| `jira attachment add` | Open the issue in the Jira issue view and look for the exact filename and size. |
| `jira transition` | Run `details.recovery_argv.inspect` (`yojo jira issue get KEY`) for the status, then `details.recovery_argv.requirements`. For a `pre_edit` edit, check the edited field values. |
| `confluence page create` | Run the searches in `error.fix` (`details.recovery_argv`): first `space = "KEY" AND type = page AND (title = "<submitted>" OR title = "<trimmed>")`, then, if that finds nothing, the space's newest pages (`space = "KEY" AND type = page ORDER BY created DESC`), following `next_start` through every result page. Read each plausible candidate with `yojo confluence page read ID --full` and compare `title`, `space_key`, `ancestors`, and `body_storage` with what you sent. An empty exact-title search alone does not show the page was not created. Results can shift between pages, so a scan with no match does not establish absence either: ask a human to confirm the page does not exist before running the create again. |
| `confluence page update` | `yojo confluence page read ID --full` and compare `version`, `title`, and `body_storage` with what you sent. |
| `bitbucket pr create` | Run `details.recovery_argv.list` (`yojo bitbucket pr list --repo PROJECT/slug --state ALL`), following `next_start` through every page, and note the ID of each pull request from your source into your target; add the ID in `details.recovery_argv.get` if the response named one. List titles are shortened, so use the list only to find candidates. Read each candidate with `yojo bitbucket pr get ID --repo PROJECT/slug --full` and count it as yours only if its `title`, `source`, `target`, and `description` equal what you sent and `created` is at or after `details.sent_at` (allow for clock skew). An older merged or declined pull request can share the branches and title. A paged list can change while you scan it, so no match does not prove absence. If no candidate matches exactly, or more than one does, ask a human to confirm in the repository before running the create again. |
| `bitbucket pr comment` | Open the pull request link in `error.fix` and look for a comment matching what you sent. The duplicate check runs before posting, so never send the same comment from two calls at once. |

Commands inside `error.fix` are quoted for POSIX shells (sh, bash, zsh) only. In `cmd.exe`,
PowerShell, or a program, do not paste them; pass each list in `details.recovery_argv` (or
`details.retry_argv` after a refusal) as separate arguments instead.

Agents inspect only through `yojo` and, where the table says issue view, by asking a human to check
it. Agents never call the REST APIs directly. As a human-only exception, a person may use an
approved read-only REST call (for example `GET /rest/api/2/issue/KEY/comment`) in place of the
issue view.

Run the write again only when inspection shows it was **not** applied. If it was applied, continue
from the existing item. If you cannot tell, or more than one candidate matches (for example two
issues with the same summary), stop and ask a human to resolve it, including deleting any
duplicate by hand. A search whose results changed or were incomplete while you read them cannot
show that a write was not applied. For `partial_transition` (exit 7) and `pre_edit` transitions,
also follow [`pre_edit` (non-atomic)](configuration.md#pre_edit-non-atomic): reconcile current
field values and get `ready: true` from `yojo jira requirements` first.
