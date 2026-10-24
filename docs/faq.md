# FAQ

<details>
<summary><strong>Why JSON only?</strong></summary>

`atl` is built for AI coding agents first. One JSON object on stdout, fixed exit codes, and a
concrete `fix` for every error mean an agent never scrapes text. People can pipe the output
through `jq` or read it directly.

</details>

<details>
<summary><strong>Does <code>atl</code> work with Atlassian Cloud?</strong></summary>

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

No. A human runs `atl login`. Secrets come only from a hidden prompt or stdin and go straight into
the OS keyring. An agent must never see, store, or pass your token.

</details>

<details>
<summary><strong>Why does story create refuse my story without acceptance criteria?</strong></summary>

Acceptance criteria are a judgment call, so the caller writes them. `atl` checks that they are
present and never generates them.

</details>

<details>
<summary><strong>Why won't <code>atl</code> pick the parent feature?</strong></summary>

Choosing a feature is a judgment call. `atl jira feature find` ranks candidates with reasons and
always returns `"selection": "caller"`. You, or a human, choose.

</details>

<details>
<summary><strong>Who is "confirmed_by" in a feature attestation? Is it verified?</strong></summary>

It is whatever the caller supplies. `atl` records it and states `"identity_verified": false`.
It is an accountability record, not proof of identity, so an agent must never invent it.

</details>

<details>
<summary><strong>A write timed out. Can I just run it again?</strong></summary>

Not until you inspect. `outcome_unknown` means the write may already have happened. Check the
issue or page as described in [Writes and retries](commands.md#writes-and-retries), and retry only
if the write was not applied.

</details>

<details>
<summary><strong>What does <code>pre_edit</code> do, and why is it risky?</strong></summary>

It lets a transition set a field that is not on the transition screen by editing the issue (PUT)
before the transition (POST). The two writes are not atomic. If the transition fails, the edit
stays. See [`pre_edit` (non-atomic)](configuration.md#pre_edit-non-atomic).

</details>

<details>
<summary><strong>Why does <code>atl</code> refuse my plaintext keyring?</strong></summary>

Credentials must be encrypted at rest by the operating system. `atl` accepts only macOS Keychain,
Windows Credential Manager, and Linux Secret Service or KWallet, and never falls back to a file.

</details>

<details>
<summary><strong>Can I disable TLS verification for a test server?</strong></summary>

No. Point `ca_bundle` at your internal CA's PEM file instead.

</details>

<details>
<summary><strong>How do I see everything a command accepts and returns?</strong></summary>

Run `atl schema '<command path>'`, for example `atl schema 'jira story create'`. It works
offline. The [command reference](reference/commands.md) renders the same schemas with examples.

</details>
