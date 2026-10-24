# atlass-cli

Agent-first CLI for Atlassian Data Center (Jira + Confluence) with team-defined workflow rules.

`atl` gives AI coding agents and developers one JSON interface to Jira and Confluence Data Center.
The fixed rules live in the CLI and in a team-owned `atl.yaml`: field IDs, required fields, what
each workflow step needs, and required attachments. Judgment calls stay with the caller. Writing
acceptance criteria and choosing a parent feature are examples. `atl` supplies checklists and
ranked candidates, but never makes those decisions.

- JSON-only output with fixed exit codes and a concrete `fix` for every error
- `atl commands` and `atl schema '<command>'` for offline discovery
- PAT or username/password stored in the OS keyring, with verified TLS
- One-call requirement checklists that gate every create and transition
- Windows, macOS, and Linux; Python 3.11+

## Install

```bash
python3.11 -m pip install .            # or: pipx install .
atl commands
```

## Set up

1. Copy [`atl.example.yaml`](atl.example.yaml) to `atl.yaml` in your repository root. Replace the
   placeholders with your URLs, projects, field names or IDs, and workflow statuses. See
   [docs/configuration.md](docs/configuration.md).
2. Log in. A human does this once per machine, and the secret goes into the OS keyring:

   ```bash
   atl login --service jira                    # hidden prompt for a personal access token
   atl login --service confluence
   # non-interactive: printf '%s\n' "$TOKEN" | atl login --service jira --token-stdin
   # basic auth:      atl login --service jira --auth basic --username jdoe
   ```

3. Check the config against live Jira:

   ```bash
   atl config check --issue EXAMPLE-123
   ```

`atl` finds `atl.yaml` by walking up from the current directory. `--config PATH` overrides that.

## Quickstart

```bash
atl jira feature find --text "single sign-on"
echo '{"project":"EXAMPLE","summary":"Add SSO button","acceptance_criteria":"Given...","feature_key":"EXAMPLE-10"}' \
  | atl jira story create --input -
atl jira requirements EXAMPLE-42 --to "In Progress"
atl jira transition EXAMPLE-42 --to "In Progress"
atl jira attachment add EXAMPLE-42 --file EXAMPLE-42_TestResults_1.xml
atl jira transition EXAMPLE-42 --to Done
atl jira comment add EXAMPLE-42 --body-file notes.txt
atl jira search --jql 'project = EXAMPLE AND status = "In Progress"' --limit 20
atl confluence page read 123456
```

Every command prints one object: `{"ok": true, "data": ...}` or
`{"ok": false, "error": {"code", "message", "fix", "details"}}`.

| Exit | Meaning |
|------|---------|
| 0 | success |
| 2 | input or config error |
| 3 | authentication or keyring error |
| 4 | unmet workflow requirement, approval required, or config drift |
| 5 | remote validation, not found, or conflict |
| 6 | transport/TLS/service failure, or unknown write outcome |
| 7 | confirmed partial write |

## Documentation

- [AGENTS.md](AGENTS.md): how agents should use `atl`, whichever tool they run in
- [docs/commands.md](docs/commands.md): command reference
- [docs/configuration.md](docs/configuration.md): `atl.yaml` reference, `pre_edit`, and drift checks
- [docs/security.md](docs/security.md): threat model and OWASP ASVS 5.0 L2 applicability

## Compatibility

`atl` targets **Jira Data Center 9+** and **Confluence Data Center 7.9+**. It uses only Data Center
REST endpoints:

- PATs: Jira 8.14+ and Confluence 7.9+
- Targeted, paginated create metadata (`/rest/api/2/issue/createmeta/{project}/issuetypes`):
  Jira 8.4+, required on Jira 9+
- `/rest/api/content` for Confluence

Atlassian Cloud is not supported.

The fixture-based tests model documented Data Center responses. **They do not prove live
compatibility.** Live status is recorded in the checklist below, which is run against a disposable
instance, never a production project.

### Live compatibility checklist

| Check | Jira DC version | Confluence DC version | Status |
|-------|-----------------|-----------------------|--------|
| PAT login (Bearer) and basic login | — | — | unverified |
| Custom CA bundle and reverse-proxy context path (`/jira`, `/confluence`) | — | — | unverified |
| Redirect refusal (SSO-fronted instance) | — | — | unverified |
| Story create with targeted createmeta and custom fields (text, Parent Link) | — | n/a | unverified |
| `config check` pass/drift/unverified on real metadata | — | n/a | unverified |
| Transition with on-screen `auto_set` (single option) | — | n/a | unverified |
| Transition with `pre_edit: true` (PUT then POST) and a forced partial failure | — | n/a | unverified |
| Attachment at the size limit and over a lower server limit; filename pattern on Done | — | n/a | unverified |
| Feature find / feature create under both policies | — | n/a | unverified |
| JQL / CQL pagination | — | — | unverified |
| Confluence create, update, stale version, and concurrent-edit 409 | n/a | — | unverified |

Record the exact product version for each row as it passes. Do not assume later versions pass.

## Development

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m mypy
.venv/bin/python -m ruff check .
.venv/bin/python -m build
.venv/bin/python -m pip_audit --skip-editable
```

On Windows, use `.venv\Scripts\python.exe`. CI runs lint, type checks, tests, and the build on
Ubuntu, Windows, and macOS, plus a dependency audit.
