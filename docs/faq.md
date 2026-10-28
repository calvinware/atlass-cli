# FAQ

<details>
<summary><strong>Is the output JSON or text?</strong></summary>

Both, from the same result. In a terminal, `yojo` shows a readable view: tables, plain-language
status, and a link after a write. When stdout is piped, or with `--json`, it prints exactly one
compact JSON object, so scripts and agents never scrape text. Exit codes are the same either way.
`NO_COLOR` turns color off.

</details>

<details>
<summary><strong>Do I need a <code>yojo.yaml</code>?</strong></summary>

No. Pass `--base-url` with your instance root and `yojo` uses Jira's own metadata for required
fields, screens, and allowed values. A `yojo.yaml` adds friendly field names, fields your team
requires on create, and transition rules. See [Configuration](configuration.md).

</details>

<details>
<summary><strong>I have an old <code>atl.yaml</code>. Will <code>yojo</code> read it?</strong></summary>

No. `yojo` stops with `config_legacy` and changes nothing. Rename the file to `yojo.yaml` and
review it as described in [Moving from `atl.yaml`](configuration.md#moving-from-atlyaml).

</details>

<details>
<summary><strong>Does <code>yojo</code> work with Atlassian Cloud?</strong></summary>

No. It targets Jira Data Center 9+ and Confluence Data Center 7.9+ and uses only Data Center REST
endpoints.

</details>

<details>
<summary><strong>Has it been tested against a live Data Center instance?</strong></summary>

Not yet. The automated tests use fixtures that model documented Data Center responses. They do not
prove live compatibility. The README's live compatibility checklist records each check as
`unverified` until someone runs it against a disposable instance.

</details>

<details>
<summary><strong>Can an agent log in for me?</strong></summary>

No. A human runs `yojo login`. Secrets come only from a hidden prompt or stdin and go straight into
the OS keyring. An agent must never see, store, or pass your token.

</details>

<details>
<summary><strong>Why was my write refused when I only made one mistake?</strong></summary>

It was refused for every problem at once, so you can fix them together. Nothing was sent.
`details.retry` (`Retry:` in a terminal) is your command with each fix applied; replace any
`<placeholder>` with real content and run it once.

</details>

<details>
<summary><strong>Will <code>yojo</code> fill in a summary, description, or required value for me?</strong></summary>

No. Content is yours to write. When a required value is missing, the retry line has a
`<placeholder>` in its place, and `yojo` never invents one.

</details>

<details>
<summary><strong>I typed a field or status slightly wrong. Will <code>yojo</code> guess?</strong></summary>

No. It refuses and says "Did you mean ...?". The suggestion goes into the retry line so you can run
it on purpose. When a name matches more than one field, the candidates are listed with their IDs.

</details>

<details>
<summary><strong>A write timed out. Can I just run it again?</strong></summary>

Not until you inspect. `outcome_unknown` means the write may already have happened, so there is no
retry line. Check the issue or page as described in
[Writes and retries](commands.md#writes-and-retries), and run it again only if the write was not
applied.

</details>

<details>
<summary><strong>What does <code>pre_edit</code> do, and why is it risky?</strong></summary>

It lets a transition set a field that is not on the transition screen by editing the issue (PUT)
before the transition (POST). The two writes are not atomic. If the transition fails, the edit
stays. See [`pre_edit` (non-atomic)](configuration.md#pre_edit-non-atomic).

</details>

<details>
<summary><strong>Why does <code>yojo</code> refuse my plaintext keyring?</strong></summary>

Credentials must be encrypted at rest by the operating system. `yojo` accepts only macOS Keychain,
Windows Credential Manager, and Linux Secret Service or KWallet, and never falls back to a file.

</details>

<details>
<summary><strong>Can I disable TLS verification for a test server?</strong></summary>

No. Point `ca_bundle` at your internal CA's PEM file instead.

</details>

<details>
<summary><strong>How do I see everything a command accepts and returns?</strong></summary>

Run `yojo commands` for every command's usage line and an example, or
`yojo schema '<command path>'` for the exact JSON Schemas, for example
`yojo schema 'jira issue create'`. Both work offline. The [command reference](reference/commands.md) renders the same schemas with examples.

</details>
