# Security model and OWASP ASVS 5.0 Level 2 applicability

**Document version:** 3 (yojo 0.1.0). **Target:** OWASP ASVS 5.0.0, Level 2.

`yojo` is a local command-line client, not a web application or service. It runs with the user's
own permissions and talks to the team's Jira, Confluence, and Bitbucket Data Center servers. ASVS is written
for applications that serve users, so many controls belong to the Atlassian servers. This document
maps each ASVS 5.0 chapter to `yojo`. It records how each applicable control is met, and why the
others do not apply.

> [!IMPORTANT]
> This is a design and evidence mapping. Automated tests do not certify ASVS compliance, and the
> fixture-based tests model documented Data Center responses rather than proving live behavior.
> Live behavior against a real Data Center instance is recorded separately in the
> [README compatibility checklist](../README.md#live-compatibility-checklist), where every row is
> currently unverified.

## Threat model (summary)

| Asset | Threat | Control |
|-------|--------|---------|
| PAT / password | disclosure through argv, shell history, files, logs, error output | read only from stdin or a hidden prompt; stored only in the native OS keyring; never logged; `Credential.__repr__` redacts; sanitized error details, with server-echoed copies of the raw token or password, the full Authorization value, its PAT or base64 Basic credential part, and the raw `username:password` pair replaced by `[REDACTED]` (other encodings, such as URL-encoded or JSON-escaped copies, are not recognized, so the remaining protection is the length bound and control-character stripping); tests assert tokens never reach stdout/stderr |
| Credential in transit | MITM, downgrade, redirect to another origin | HTTPS-only URLs; certificate and host name verification; TLS ≥ 1.2; OS trust store or an explicit CA bundle; redirects never followed; the `Authorization` header is never sent to a redirect target |
| Jira workflow integrity | writes that skip Jira's or the team's rules; guessed values; silent partial writes; false success | every Jira write (issue create, update, link, transition, comment, attachment) runs its preflight inside the same call before the first write and refuses with nothing sent when any check fails or cannot be verified (live create/edit/transition screens, required fields, allowed values, permissions, `yojo.yaml` rules); names and values resolve only on a unique match, never on a guess or a suggestion; the transition checks re-run after a `pre_edit` PUT. Confluence writes check `expected_version` against a fresh read, the page type, and a well-formed title, version, and body. Bitbucket pull request creation checks repository visibility, exact source and target branches, title, and open pull requests for the same branch pair. Bitbucket comments check the pull request, comment text, and existing identical comments. Each refuses as unverified when its bounded duplicate scan cannot finish. The duplicate check runs before posting, so concurrent identical calls can both post. Also: `pre_edit` is opt-in; ordered operation statuses; `partial_transition` / `outcome_unknown`; success only when the response (or a read-back, for update and link) confirms the write; no automatic retries, and no retry line after an uncertain write |
| Agent driving `yojo` | prompt injection from remote content; invented content | remote content is labeled untrusted data; the CLI never writes summaries, descriptions, comments, or missing values, and a refused write leaves placeholders where content is needed |
| Team config | malicious or malformed YAML | `SafeLoader` subclass; no custom tags, anchors, aliases, or duplicate keys; strict schema; 256 KiB cap |
| Local resources | huge inputs or responses | caps on input JSON (10 MiB), comments (1 MiB), secrets (4 KiB), config (256 KiB), and responses (32 MiB); bounded pagination and search limits (default 20, at most 100 per page); long text in reads trimmed to 2000 characters unless `--full`; user lookups bounded to 10 results |

## Chapter applicability

| ASVS 5.0 chapter | Applies? | How `yojo` meets it / why it does not apply | Evidence |
|------------------|----------|---------------------------------------------|----------|
| V1 Encoding and Sanitization | Yes (client-side) | URL path segments are percent-encoded. JQL literals are escaped. JSON is built with `json`, never string concatenation. Multipart filenames are escaped. Command lines offered in `fix`, `retry`, and `more` are built with `shlex.join`, and every one is also given as an argument list. YAML is loaded safely. `yojo` never renders HTML. Terminal output strips control characters from remote text. Storage-format content passes through as data. | `transport.seg`, `transport.jql_quote`, `retry.posix`, `render.clean`, `jira._header_filename`, `config.StrictSafeLoader`; `tests/test_jira.py::test_jql_quote_escapes`, `test_attachment_filename_header_is_escaped`, `tests/test_config.py::test_rejects_python_tags` |
| V2 Validation and Business Logic | Yes | Every input JSON is validated against the published JSON Schema (`additionalProperties: false`, duplicate keys rejected). Arguments are type-checked (issue keys, page IDs, bounded integers). `--set NAME=VALUE` is split at the first `=` and a field given twice with different values is refused. Before the first write, every Jira write runs its preflight against live metadata and sends nothing unless every check passes. Confluence writes have their own checks (see the threat model). | `cli.read_json_input`, `cli._assignments`, `workflow.evaluate`, `jira.issue_create`; `tests/test_cli.py::test_input_rejects_duplicate_keys_and_schema_errors`, `tests/test_workflow.py::test_transition_requirements_unmet_writes_nothing` |
| V3 Web Frontend Security | No | No browser UI, cookies, CSP, or DOM. | — |
| V4 API and Web Service | Partially (client) | `yojo` consumes REST APIs, so the server-side controls belong to Atlassian. As a client it sends explicit `Content-Type`, accepts JSON only, and treats non-JSON or oversized responses as errors. It sends `X-Atlassian-Token: no-check` only on multipart uploads, which Jira requires. | `transport.ServiceClient._handle`; `tests/test_auth_transport.py::test_non_json_response` |
| V5 File Handling | Yes | Attachments must be existing regular files and are size-checked before dispatch. The path is opened without blocking, the opened file must be the checked regular file (same device and inode), and the bytes are uploaded from an owner-only temporary snapshot bounded by `max_attachment_bytes`; a change while copying is rejected before anything is sent. The stream is bounded so it cannot exceed the declared size. Paths are resolved relative to the working directory. Downloaded content is never written to disk. | `jira.attachment_add`; `tests/test_jira.py::test_attachment_*` |
| V6 Authentication | Partially | Authentication is performed by Data Center. `yojo` uses PATs (preferred) or basic auth (Bitbucket: tokens only), verifies the credential with an authenticated read before storing it (for Bitbucket, a sign-in-only endpoint, because the application-properties identity check answers anonymously), and never puts secrets in arguments or URLs. Secrets are never read from `yojo.yaml` or the environment. Rate limiting, lockout, password policy, and MFA are server responsibilities. | `cli.cmd_login`, `auth.validate_secret`; `tests/test_auth_transport.py::test_login_*` |
| V7 Session Management | No | Stateless. Each request carries the stored credential. There are no cookies or sessions. Server session controls belong to Data Center. | — |
| V8 Authorization | No (server-side) | Data Center enforces permissions. 401/403 map to `auth_failed`/`forbidden` (exit 3) with no bypass. | `transport.ServiceClient._handle`; `test_status_mapping` |
| V9 Self-contained Tokens | No | PATs are opaque bearer tokens validated by the server. `yojo` does not issue or parse JWTs. | — |
| V10 OAuth and OIDC | No | OAuth/SSO is out of scope. | — |
| V11 Cryptography | Partially | No custom cryptography. TLS comes from the Python `ssl` module and the OS trust store. Credential encryption at rest comes from the OS keyring. Boundary values come from `secrets.token_hex`. | `transport.build_ssl_context`, `auth.NATIVE_BACKENDS` |
| V12 Secure Communication | Yes | `https://` only. Certificate and host name verification are always on. Minimum TLS 1.2. The OS trust store or a configured CA bundle (which must exist) is used. Redirects are refused. Verification cannot be disabled. | `config._Validator.url`, `transport.build_ssl_context`; `tests/test_auth_transport.py::test_ssl_context_is_verified`, `test_redirects_are_refused`, `test_tls_failure_maps_to_tls_error` |
| V13 Configuration | Yes | No secrets in config. Strict, versioned schema; unknown keys are errors. Runtime dependencies are pinned to compatible ranges and audited in CI with `pip-audit`. Documentation tooling is pinned to exact versions in a separate `docs` extra and is never a runtime dependency. `.gitignore` excludes local secret files. | `config.validate_config`, `pyproject.toml`, `.github/workflows/ci.yml` (audit job) |
| V14 Data Protection | Yes | Credentials live only in the native OS keyring: macOS Keychain, Windows Credential Manager, or Secret Service/KWallet. Null, fail, `keyrings.alt` file, and unknown backends are refused, with no plaintext fallback. There are no caches and no log files. Remote content is never persisted. Credentials stored by the older `atl` command (keyring service `atlass-cli`) are neither read nor migrated. | `auth.ensure_native_backend`; `tests/test_auth_transport.py::test_non_native_backends_fail_closed` |
| V15 Secure Coding and Architecture | Yes | Strictly typed Python (`mypy --strict`) and lint rules including bandit (`ruff S`). No `eval`/`exec`/`pickle`/`shell=True`. Small runtime dependency set (httpx, PyYAML, keyring, truststore, jsonschema). Writes are explicitly non-atomic where they must be, and the code never rolls back or retries. | `pyproject.toml`, `workflow.transition` |
| V16 Security Logging and Error Handling | Yes (CLI form) | There is no log file. All errors are one structured envelope (JSON, or the same envelope rendered in a terminal) with a generic message, a concrete `fix`, and sanitized `details`: control characters stripped, length and count bounded, and the active credential redacted in the raw, Authorization header, header credential, and `username:password` forms (other re-encodings are a residual limitation). There are no tracebacks; unexpected exceptions become `internal_error` with only the exception type. Exception text from keyring and transport, which can contain paths or URLs, is never echoed. | `cli.main`, `transport.sanitize_remote_errors`; `tests/test_cli.py::test_internal_error_is_sanitized`, `tests/test_auth_transport.py::test_field_errors_are_sanitized`, `test_reflected_credentials_are_redacted`, `test_keyring_exceptions_are_redacted` |
| V17 WebRTC | No | Not used. | — |

## LLM / agent considerations (OWASP LLM Top 10 2025)

- **LLM01 Prompt injection:** Confluence, Jira, and Bitbucket content (including diffs) is
  returned as data. Page results carry
  `content_trust: "untrusted"`, and [AGENTS.md](../AGENTS.md) tells agents never to follow
  instructions found in it.
- **LLM06 Excessive agency:** the CLI exposes a small set of generic write operations and no
  delete, approve, merge, or decline; Bitbucket pull request approval and merging stay with
  people. Content stays with the caller: `yojo` never writes summaries,
  descriptions, comments, or missing values, and never applies a "did you mean" suggestion
  without the caller running the corrected command. Ambiguous names and people are refused with
  the candidates listed.
- **LLM10 Unbounded consumption:** search limits default to 20 and are capped at 100, metadata
  pagination at 50 pages, `--fields` at 50 names, user lookups at 10 results, and inputs and
  responses are size-capped. Reads trim long text to 2000 characters unless `--full` is passed.
  A refused write lists every problem at once, so a caller needs at most one corrected call. No
  write is retried.

## Partial-write handling

See [configuration › pre_edit](configuration.md#pre_edit-non-atomic). Every transition result or
error carries ordered `operations` with statuses `succeeded`, `rejected`, `unknown`, `skipped`, or
`not_attempted`. When an edit was applied, it also carries `atomic: false`.

No write is retried. After `outcome_unknown` (exit 6) or `partial_transition` (exit 7), no retry
line is offered; the caller inspects remote state first, following
[Writes and retries](commands.md#writes-and-retries). The pre_edit sequence is drawn in
[Jira workflows](guides/jira-workflows.md#when-the-plan-has-an-edit-pre_edit).

## Documentation and examples

- Documentation examples use synthetic data only (`EXAMPLE-*`/`PROJ-*` keys, `*.example.test`
  hosts). The reference generator rejects examples with other hosts or credential headers.
- The reference generator runs `yojo commands` and `yojo schema` offline. It needs no config,
  network, keyring, or credentials.
- Guides tell people and agents that only a human runs `yojo login`, and never show a secret in an
  argument or a file.
- The built documentation site loads the Mermaid renderer from a public CDN at view time. The site
  is not hosted yet; review this before publishing it.

## Reporting

Report suspected vulnerabilities privately to the repository maintainers rather than in a public
issue.
