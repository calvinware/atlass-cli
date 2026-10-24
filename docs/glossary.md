# Glossary

| Term | Meaning |
|------|---------|
| Acceptance criteria | The conditions a story must meet, written by the caller. Story create requires them and maps them to the `acceptance_criteria` field. `atl` never writes them. |
| `atl.yaml` | The team-owned, version-1 config file: projects, field mappings, create requirements, transitions, and feature policy. Found by walking up from the current directory. See the [configuration reference](configuration.md). |
| Attestation | The `human_confirmation` object (`confirmed_by`, `statement`) that feature create needs under `human_confirmed`. Recorded text only; `identity_verified` is always `false`. |
| `auto_set` | Values a transition sets, keyed by friendly field name. The only source of transition values. |
| Check | One item in a checklist or drift report, with `id`, `status` (`pass`, `fail`, `drift`, `unverified`, or `not_applicable`), `message`, and `fix`. |
| Checklist | The result of `atl jira requirements`: `ready`, every check, and the planned writes (`plan.operations`). |
| Content trust | `"content_trust": "untrusted"` on Confluence results, a reminder that remote text is data, never instructions. |
| Create preflight | The checks story and feature create run before sending anything. Any failure is `requirements_unmet` or `approval_required`, and nothing is sent. |
| Drift | A difference between `atl.yaml` and live Jira metadata, reported by `atl config check`. |
| Encoding | How a friendly field's value is serialized for Jira: `text`, `issue_key_string`, `issue_key_object`, `single_option`, `multi_option`, or `resolution_option`. |
| Envelope | The single JSON object every command prints: `{"ok": true, "data": ...}` or `{"ok": false, "error": {...}}`. |
| Feature | The parent issue type for stories, configured per project as `feature_type` (it can be any issue type, such as Epic). |
| Feature create policy | `human_confirmed` (default) requires an attestation to create a feature; `automatic` does not. |
| Friendly name | A lowercase snake_case name in `atl.yaml` (for example `work_started`) that maps to exactly one Jira field by `jira_name` or `id`. |
| Outcome unknown | A write that was dispatched but not confirmed (`outcome_unknown`, exit 6). Inspect before any retry. |
| Partial transition | A `pre_edit` transition whose edit was applied but whose transition was not (`partial_transition`, exit 7). |
| PAT | A Data Center personal access token, sent as `Authorization: Bearer`. The preferred login method. |
| `pre_edit` | An opt-in transition setting that edits off-screen fields (PUT) before the transition (POST). Not atomic. |
| Storage format | Confluence's XHTML-based page markup, used for `body_storage`. |
