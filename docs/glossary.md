# Glossary

| Term | Meaning |
|------|---------|
| Alias | A lowercase snake_case name in `yojo.yaml` (for example `points`) that maps to exactly one Jira field by `jira_name` or `id`. Usable anywhere a field name is accepted. Project aliases override global ones. |
| `auto_set` | Values a `yojo.yaml` transition rule sets during the transition, keyed by field name or alias. |
| Check | One item in a refusal, checklist, or drift report, with `id`, `status` (`pass`, `fail`, `drift`, `unverified`, or `not_applicable`), `message`, and `fix`. |
| Checklist | The result of `yojo jira requirements`: `ready`, every check, and the planned writes (`plan.operations`). `transition` runs the same checks itself. |
| Content trust | `"content_trust": "untrusted"` on read results, a reminder that remote text is data, never instructions. |
| Drift | A difference between `yojo.yaml` and live Jira, reported by `yojo config check`. |
| Envelope | The single JSON object every command prints in JSON mode: `{"ok": true, "data": ...}` or `{"ok": false, "error": {...}}`. The terminal view renders the same object. |
| JSON mode | Output for programs: chosen with `--json`, or automatically when stdout is not a terminal. |
| Outcome unknown | A write that was dispatched but not confirmed (`outcome_unknown`, exit 6). Inspect before running it again. |
| Partial transition | A `pre_edit` transition whose edit was applied but whose transition was not (`partial_transition`, exit 7). |
| PAT | A Data Center personal access token, sent as `Authorization: Bearer`. The preferred login method. |
| Placeholder | A `<value>`-style slot in a retry line where a person or agent must supply real content. |
| Preflight | The checks every Jira write runs before sending anything. Any failure is `requirements_unmet` (exit 4), and nothing is sent. |
| `pre_edit` | An opt-in transition setting that edits off-screen fields (PUT) before the transition (POST). Not atomic. |
| Retry line | `details.retry` on a refused write: the full corrected command, with suggestions applied and placeholders where content is missing. Never offered after `outcome_unknown` or `partial_transition`. |
| Storage format | Confluence's XHTML-based page markup, used for `body_storage`. |
| `yojo.yaml` | The optional, team-owned, version-1 config file: URLs, aliases, create requirements, and transition rules. Found by walking up from the current directory. See the [configuration reference](configuration.md). |
