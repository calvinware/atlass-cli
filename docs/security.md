# Security model and OWASP ASVS 5.0 Level 2 applicability

**Document version:** 2 (atl 1.0.0). **Target:** OWASP ASVS 5.0.0, Level 2.

`atl` is a local command-line client, not a web application or service. It runs with the user's
own permissions and talks to the team's Jira and Confluence Data Center servers. ASVS is written
for applications that serve users, so many controls belong to the Atlassian servers. This document
maps each ASVS 5.0 chapter to `atl`. It records how each applicable control is met, and why the
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
| Jira workflow integrity | writes that skip team rules; silent partial writes; false success | Jira story/feature create run the create preflight and transitions run the workflow checklist (`atl jira requirements`) before the first write; the transition checklist re-runs after a `pre_edit` PUT. Comments, attachments, and Confluence writes are not governed by the workflow checklist; they have their own checks (non-empty comment body within 1 MiB; attachment is an existing regular file within `max_attachment_bytes`; Confluence `expected_version` against a fresh read, page type, and a well-formed title, version, and body). Also: `pre_edit` is opt-in; ordered operation statuses; `partial_transition` / `outcome_unknown`; success only when the response confirms the write; no automatic retries |
| Agent driving `atl` | prompt injection from remote content; invented approvals | remote content is labeled untrusted data; the CLI never writes acceptance criteria or picks features; `human_confirmation` is an attestation, never identity proof |
| Team config | malicious or malformed YAML | `SafeLoader` subclass; no custom tags, anchors, aliases, or duplicate keys; strict schema; 256 KiB cap |
| Local resources | huge inputs or responses | caps on input JSON (10 MiB), comments (1 MiB), secrets (4 KiB), config (256 KiB), and responses (32 MiB); bounded pagination and search limits |

## Chapter applicability

| ASVS 5.0 chapter | Applies? | How `atl` meets it / why it does not apply | Evidence |
|------------------|----------|---------------------------------------------|----------|
| V1 Encoding and Sanitization | Yes (client-side) | URL path segments are percent-encoded. JQL literals are escaped and search words are reduced to alphanumerics. JSON is built with `json`, never string concatenation. Multipart filenames are escaped. YAML is loaded safely. `atl` never renders HTML. Storage-format content passes through as data. | `transport.seg`, `features.jql_quote`, `jira._header_filename`, `config.StrictSafeLoader`; `tests/test_jira.py::test_jql_quote_escapes`, `test_attachment_filename_header_is_escaped`, `tests/test_config.py::test_rejects_python_tags` |
| V2 Validation and Business Logic | Yes | Every input JSON is validated against the published JSON Schema (`additionalProperties: false`, duplicate keys rejected). Arguments are type-checked (issue keys, page IDs, bounded integers). Before the first write, Jira create runs the create preflight and Jira transition runs the workflow checklist; neither sends a write unless every check passes. Comment, attachment, and Confluence writes are validated by their own command-specific checks (see the threat model), not by the workflow checklist. | `cli.read_json_input`, `workflow.evaluate`; `tests/test_cli.py::test_input_rejects_duplicate_keys_and_schema_errors`, `tests/test_workflow.py::test_transition_requirements_unmet_writes_nothing` |
| V3 Web Frontend Security | No | No browser UI, cookies, CSP, or DOM. | — |
| V4 API and Web Service | Partially (client) | `atl` consumes REST APIs, so the server-side controls belong to Atlassian. As a client it sends explicit `Content-Type`, accepts JSON only, and treats non-JSON or oversized responses as errors. It sends `X-Atlassian-Token: no-check` only on multipart uploads, which Jira requires. | `transport.ServiceClient._handle`; `tests/test_auth_transport.py::test_non_json_response` |
| V5 File Handling | Yes | Attachments must be existing regular files and are size-checked before dispatch. The path is opened without blocking, the opened file must be the checked regular file (same device and inode), and the bytes are uploaded from an owner-only temporary snapshot bounded by `max_attachment_bytes`; a change while copying is rejected before anything is sent. The stream is bounded so it cannot exceed the declared size. Paths are resolved relative to the working directory. Downloaded content is never written to disk. | `jira.attachment_add`; `tests/test_jira.py::test_attachment_*` |
| V6 Authentication | Partially | Authentication is performed by Data Center. `atl` uses PATs (preferred) or basic auth, verifies the credential with an authenticated read before storing it, and never puts secrets in arguments or URLs. Rate limiting, lockout, password policy, and MFA are server responsibilities. | `cli.cmd_login`, `auth.validate_secret`; `tests/test_auth_transport.py::test_login_*` |
| V7 Session Management | No | Stateless. Each request carries the stored credential. There are no cookies or sessions. Server session controls belong to Data Center. | — |
| V8 Authorization | No (server-side) | Data Center enforces permissions. 401/403 map to `auth_failed`/`forbidden` (exit 3) with no bypass. | `transport.ServiceClient._handle`; `test_status_mapping` |
| V9 Self-contained Tokens | No | PATs are opaque bearer tokens validated by the server. `atl` does not issue or parse JWTs. | — |
| V10 OAuth and OIDC | No | OAuth/SSO is out of scope for V1. | — |
| V11 Cryptography | Partially | No custom cryptography. TLS comes from the Python `ssl` module and the OS trust store. Credential encryption at rest comes from the OS keyring. Boundary values come from `secrets.token_hex`. | `transport.build_ssl_context`, `auth.NATIVE_BACKENDS` |
| V12 Secure Communication | Yes | `https://` only. Certificate and host name verification are always on. Minimum TLS 1.2. The OS trust store or a configured CA bundle (which must exist) is used. Redirects are refused. Verification cannot be disabled. | `config._Validator.url`, `transport.build_ssl_context`; `tests/test_auth_transport.py::test_ssl_context_is_verified`, `test_redirects_are_refused`, `test_tls_failure_maps_to_tls_error` |
| V13 Configuration | Yes | No secrets in config. Strict, versioned schema; unknown keys are errors. Runtime dependencies are pinned to compatible ranges and audited in CI with `pip-audit`. Documentation tooling is pinned to exact versions in a separate `docs` extra and is never a runtime dependency. `.gitignore` excludes local secret files. | `config.validate_config`, `pyproject.toml`, `.github/workflows/ci.yml` (audit job) |
| V14 Data Protection | Yes | Credentials live only in the native OS keyring: macOS Keychain, Windows Credential Manager, or Secret Service/KWallet. Null, fail, `keyrings.alt` file, and unknown backends are refused, with no plaintext fallback. There are no caches and no log files. Remote content is never persisted. | `auth.ensure_native_backend`; `tests/test_auth_transport.py::test_non_native_backends_fail_closed` |
| V15 Secure Coding and Architecture | Yes | Strictly typed Python (`mypy --strict`) and lint rules including bandit (`ruff S`). No `eval`/`exec`/`pickle`/`shell=True`. Small runtime dependency set (httpx, PyYAML, keyring, truststore, jsonschema). Writes are explicitly non-atomic where they must be, and the code never rolls back or retries. | `pyproject.toml`, `workflow.transition` |
| V16 Security Logging and Error Handling | Yes (CLI form) | There is no log file. All errors are structured JSON with a generic message, a concrete `fix`, and sanitized `details`: control characters stripped, length and count bounded, and the active credential redacted in the raw, Authorization header, header credential, and `username:password` forms (other re-encodings are a residual limitation). There are no tracebacks; unexpected exceptions become `internal_error` with only the exception type. Exception text from keyring and transport, which can contain paths or URLs, is never echoed. | `cli.main`, `transport.sanitize_remote_errors`; `tests/test_cli.py::test_internal_error_is_sanitized`, `tests/test_auth_transport.py::test_field_errors_are_sanitized`, `test_reflected_credentials_are_redacted`, `test_keyring_exceptions_are_redacted` |
| V17 WebRTC | No | Not used. | — |

## LLM / agent considerations (OWASP LLM Top 10 2025)

- **LLM01 Prompt injection:** Confluence and Jira content is returned as data. Page results carry
  `content_trust: "untrusted"`, and [AGENTS.md](../AGENTS.md) tells agents never to follow
  instructions found in it.
- **LLM06 Excessive agency:** the CLI exposes only the V1 write operations. Judgment calls stay
  with the caller: acceptance criteria, feature choice, and human approval for features.
  `human_confirmed` blocks feature creation until an attestation is supplied, and the result
  states `identity_verified: false`.
- **LLM10 Unbounded consumption:** search limits are capped at 100 (25 for feature find), metadata
  pagination at 50 pages, and inputs and responses are size-capped. No write is retried.

## Partial-write handling

See [configuration › pre_edit](configuration.md#pre_edit-non-atomic). Every transition result or
error carries ordered `operations` with statuses `succeeded`, `rejected`, `unknown`, `skipped`, or
`not_attempted`. When an edit was applied, it also carries `atomic: false`.

No write is retried. After `outcome_unknown` (exit 6) or `partial_transition` (exit 7), the caller
inspects remote state before any retry, following
[Writes and retries](commands.md#writes-and-retries). The pre_edit sequence is drawn in
[Jira workflows](guides/jira-workflows.md#when-the-plan-has-an-edit-pre_edit).

## Documentation and examples

- Documentation examples use synthetic data only (`EXAMPLE-*` keys, `*.example.test` hosts). The
  reference generator rejects examples with other hosts or credential headers.
- The reference generator runs `atl commands` and `atl schema` offline. It needs no config,
  network, keyring, or credentials.
- Guides tell people and agents that only a human runs `atl login`, and never show a secret in an
  argument or a file.
- The built documentation site loads the Mermaid renderer from a public CDN at view time. The site
  is not hosted yet; review this before publishing it.

## Reporting

Report suspected vulnerabilities privately to the repository maintainers rather than in a public
issue.
