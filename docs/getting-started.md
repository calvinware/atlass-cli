# Getting started

This page takes you from a fresh machine to a working `yojo` setup on Windows, macOS, or Linux.
You need Python 3.11 or later, network access to your Jira, Confluence, or Bitbucket Data Center
servers, and a native OS keyring. CI tests Python 3.11, 3.13, 3.14, and 3.15 on all three systems; 3.12
is not a CI matrix entry.

> [!IMPORTANT]
> A **human** runs `yojo login`. The secret goes from a hidden prompt (or stdin) straight into the
> OS keyring. Never put a token or password in a command argument, a file, an environment
> variable an agent can read, or a chat with an AI tool.

## Install

`yojo` is being prepared for its first PyPI release as `yojo`. Until that release is on
[PyPI](https://pypi.org/project/yojo/), the commands below cannot find the package; install
[from source](#from-source-contributors) instead. Once it is published, install it with either one
of these commands. Each gives `yojo` an isolated environment, links the command into a user `bin`
folder, and works the same on Windows, macOS, and Linux:

```bash
pipx install yojo
# or
uv tool install yojo
```

[`pipx`](https://pipx.pypa.io/) and [`uv`](https://docs.astral.sh/uv/) must already be installed
to use them. If `yojo` is not found afterwards, that folder is not on your `PATH` yet: run
`pipx ensurepath` or `uv tool update-shell`, then open a new terminal and run `yojo commands`.

Without `pipx` or `uv`, install into a virtual environment with `pip`. You need only Python 3.11
or later:

<!-- os-tabs:start -->

### Windows

Install Python 3.11+ from python.org or the Microsoft Store, then in PowerShell:

```powershell
py -3 --version                     # must be 3.11 or later
py -3 -m venv "$env:LOCALAPPDATA\yojo\venv"
& "$env:LOCALAPPDATA\yojo\venv\Scripts\python.exe" -m pip install yojo
& "$env:LOCALAPPDATA\yojo\venv\Scripts\yojo.exe" commands
```

`py -3` runs the newest Python 3 you have installed. If it is older than 3.11, name a newer one
instead, for example `py -3.13`. If you have no `py` launcher (common with the Microsoft Store
build), use `python` in its place.

To run `yojo` by name, append the environment's `Scripts` folder to your user `PATH`, then open a
new terminal:

```powershell
$scripts = "$env:LOCALAPPDATA\yojo\venv\Scripts"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
[Environment]::SetEnvironmentVariable("Path", "$userPath;$scripts", "User")
```

### macOS

Install Python 3.11+ (for example `brew install python@3.13`, pyenv, or python.org), then:

```bash
python3 --version                   # must be 3.11 or later
python3 -m venv ~/.local/share/yojo/venv
~/.local/share/yojo/venv/bin/python -m pip install yojo
~/.local/share/yojo/venv/bin/yojo commands
```

If `python3 --version` is older than 3.11, use the newer interpreter by name, for example
`python3.13 -m venv ...`.

To run `yojo` by name, append the environment's `bin` folder to `PATH` in `~/.zshrc` (the default
shell), then open a new terminal:

```bash
echo 'export PATH="$PATH:$HOME/.local/share/yojo/venv/bin"' >> ~/.zshrc
```

### Linux

Install Python 3.11+ from your distribution or pyenv, then:

```bash
python3 --version                   # must be 3.11 or later
python3 -m venv ~/.local/share/yojo/venv
~/.local/share/yojo/venv/bin/python -m pip install yojo
~/.local/share/yojo/venv/bin/yojo commands
```

If `python3 --version` is older than 3.11, use the newer interpreter by name, for example
`python3.11 -m venv ...`. On Debian and Ubuntu, if `venv` reports that `ensurepip` is not
available, install the matching package first (for example `sudo apt install python3.11-venv`).

To run `yojo` by name, append the environment's `bin` folder to `PATH` in your shell profile
(`~/.bashrc` for bash), then open a new terminal:

```bash
echo 'export PATH="$PATH:$HOME/.local/share/yojo/venv/bin"' >> ~/.bashrc
```

<!-- os-tabs:end -->

The `PATH` entry goes last, so the environment's own `python` never shadows the one you already
use.

`yojo commands` works offline with no config. In a terminal it lists every command with an
example; piped (or with `--json`) it prints one JSON object. If you see the list, or
`{"ok":true,...}`, the install works.

### From source (contributors)

To run unreleased changes, install from a checkout instead of PyPI. Clone the repository and
replace `yojo` with `.` in any of the install commands above, for example:

```bash
git clone https://github.com/calvinware/yojo.git
cd yojo
pipx install .
```

For an editable development environment with the test and docs tools, see
[Contributing](contributing.md#development-setup).

## Keyring prerequisites

`yojo` stores credentials only in a native OS keyring. It refuses plaintext, null, and unknown
keyring backends, and it never falls back to a file. If no native keyring is usable, every
command that needs a credential fails with
[`keyring_unavailable`](reference/errors.md#keyring_unavailable) (exit 3).

<!-- os-tabs:start -->

### Windows

**Windows Credential Manager** is built in. Nothing to install. Entries are stored per Windows
user under the service name `yojo`. You can see them in *Control Panel > Credential Manager >
Windows Credentials*.

### macOS

**macOS Keychain** is built in. The login keychain must be unlocked, which it is in a normal
desktop session. The first time `yojo` reads its entry, macOS may ask whether Python may use the
keychain item; choose *Always Allow* for the interpreter that runs `yojo`.

Over SSH the login keychain is usually locked. Unlock it first, or run `yojo` from a desktop
session.

### Linux

`yojo` uses the **Secret Service** API, provided by GNOME Keyring or KWallet, over a D-Bus session.
Desktop sessions normally have one running and unlocked.

- Headless servers, containers, and CI runners usually have no Secret Service. `yojo` reports
  `keyring_unavailable` there by design.
- Over SSH, you need a D-Bus session and an unlocked keyring daemon. Running `yojo` from your
  desktop session is simpler.
- Do not install `keyrings.alt` or configure a plaintext backend to work around this. `yojo`
  refuses them.

<!-- os-tabs:end -->

## Set up in a terminal: `yojo setup`

In a terminal, `yojo setup` opens a full-screen app that walks through the whole setup. Running
plain `yojo` the first time (no `yojo.yaml` found) opens it too. It has seven steps:

1. **Connect**: enter each instance URL; `yojo` checks that it answers and shows its version.
2. **Log in**: paste a personal access token into a masked field. `yojo` confirms who you are,
   stores the token in the OS keyring, and clears the field.
3. **Choose projects**: search your live Jira projects and Bitbucket repositories.
4. **Map fields and rules** (optional): pick fields and statuses by their Jira names, with a
   plain-English preview of each rule. Nothing is added unless you add it.
5. **Review and save**: the exact `yojo.yaml` and its diff against any existing file. Only
   **Save** writes it, and it refuses if the file or Jira changed after you reviewed it.
6. **Connect your agent**: pick the tools you use; this runs `yojo agent install`.
7. **Try it**: run one read-only command and see the result. Setup completes only after Save and
   a successful Try on the saved configuration.

Use the keyboard throughout: Tab moves between fields, Enter presses a button, Space selects a
list item, Ctrl+N and Ctrl+B go to the next and previous step, Ctrl+T switches light and dark,
and Esc leaves. Leaving before Save changes no file; logins you already stored stay in the
keyring. The app follows `NO_COLOR`.

`yojo setup` never starts when stdout is piped or with `--json`. It then returns
[`usage_error`](reference/errors.md#usage_error) (exit 2) listing the same steps as commands,
which are also what scripts and CI use: `yojo login`, `yojo --json init ... --save`, and
`yojo --json agent install`. The rest of this page covers those commands.

## Log in (human only)

Point `yojo` at your instance root, including any context path such as `/jira`:

```bash
yojo login --service jira --base-url https://jira.example.test/jira   # hidden token prompt
yojo login --service confluence --base-url https://confluence.example.test/confluence
yojo login --service bitbucket --base-url https://bitbucket.example.test/bitbucket
```

If your repository already has a `yojo.yaml` with the URLs, `--base-url` can be left out, and
`yojo login --service all` logs in to Jira and Confluence. Log in to Bitbucket separately.

Bitbucket accepts a token only. Its login first reads application properties to confirm the
server, then uses an authenticated repository listing to confirm the token before storing it.
Use a token with repository write permission for pull request actions; successful login alone
does not verify that permission.

The login flow verifies the credential before anything is stored:

```mermaid
sequenceDiagram
    autonumber
    actor Human
    participant yojo
    participant Keyring as OS keyring
    participant Server as Data Center service
    Human->>yojo: yojo login --service jira --base-url URL
    yojo->>Keyring: Native backend available?
    alt Plaintext, null, or unknown backend
        yojo-->>Human: keyring_unavailable (exit 3), nothing stored
    end
    yojo-->>Human: Hidden prompt (JSON on stderr when piped)
    Human->>yojo: Token (hidden, or one line on stdin)
    yojo->>Server: Jira /myself, Confluence /user/current, or Bitbucket properties then authenticated repos
    alt 2xx with an authenticated user
        Server-->>yojo: User name
        yojo->>Keyring: Store under (service, base URL)
        yojo-->>Human: stored, logged in as ...
    else 401 or anonymous user
        Server-->>yojo: Rejected
        yojo-->>Human: auth_failed (exit 3), nothing stored
    end
```

Options:

- `--auth pat` (default) sends `Authorization: Bearer`. Use a Data Center personal access token.
- `--auth basic --username NAME` uses HTTP Basic with a password for Jira or Confluence. Prefer
  PATs, especially when SSO fronts the server. Bitbucket accepts token authentication only.
- `--token-stdin` / `--password-stdin` read one secret line per service from stdin, for a human
  piping from a password manager. Without a TTY, one of these flags is required.
- `--service all` logs in to Jira and Confluence using the URLs in `yojo.yaml`. A failure is
  [`login_failed`](reference/errors.md#login_failed) with per-service results. Use
  `--service bitbucket` for Bitbucket.

The keyring entry is keyed by service and normalized base URL, so changing the URL needs a new
login. Logins made with the older `atl` command are not reused.

## Use it

No config is needed. Pass the URL you logged in with:

```bash
yojo jira issue get PROJ-123 --base-url https://jira.example.test/jira
yojo jira search --jql 'project = PROJ AND status = "In Progress"' --base-url https://jira.example.test/jira
```

In a terminal you get a readable summary with a link. Piped, or with `--json`, you get one JSON
object, which is what scripts and agents should use.

## Optional: add `yojo.yaml`

A `yojo.yaml` in the repository root saves typing `--base-url` and adds your team's words and
rules: friendly field names, fields required on create, and what each transition needs. `yojo`
finds it by walking up from the current directory; `--config PATH` overrides that. The file never
contains credentials.

`yojo init` drafts one from live metadata, using the logins you already stored. It checks every
URL, project, and repository you name, shows the YAML, and writes nothing until you add `--save`:

```bash
yojo init --jira-url https://jira.example.test/jira --project PROJ \
  --bitbucket-url https://bitbucket.example.test/bitbucket --repo PROJ/web
yojo init --jira-url https://jira.example.test/jira --project PROJ \
  --bitbucket-url https://bitbucket.example.test/bitbucket --repo PROJ/web --save
```

`--save` never overwrites a file. To replace an existing `yojo.yaml`, preview first, then add
`--save --replace --expected-sha256 HEX` with the preview's `existing_sha256`; a file that changed
in between is refused. Close any editor that may write `yojo.yaml` first: a change made in the
moment between the final check and the replacement is not detected and can be lost. The draft holds only the URLs and project keys you named. Aliases and
rules are added only when you pass them with `--policy-input` (or `--input`; see
[Configuration](configuration.md#draft-it-with-yojo-init)), and every name in them is checked
against live Jira. To write the file by hand instead, start from
[`yojo.example.yaml`](../yojo.example.yaml) and the [cookbook](admin-cookbook.md).

If your instance uses a private CA, pass `--ca-bundle certs/team-ca.pem` to `init`; it uses that
bundle for the live check and adds it to the draft. Pass the same option to the first `yojo login`.
See [Configuration](configuration.md#draft-it-with-yojo-init).

```bash
yojo config check          # read-only: does yojo.yaml still match live Jira?
yojo jira transition PROJ-123 --to "In Progress"
```

`pass` everywhere means `yojo.yaml` matches live Jira. `drift` or `unverified` checks each carry a
`fix`; see [Troubleshooting](troubleshooting.md).

## Connect your agent

`yojo agent install` writes the instructions where your AI coding tool looks for them. The text is
generated from the command list, so it always matches this version of `yojo`, and it carries every
command's signature and an example, so the agent needs no lookup call first:

```bash
yojo agent install --target agents --target claude
```

| `--target` | File |
|------------|------|
| `agents` | `AGENTS.md` (read by many tools) |
| `claude` | `.claude/skills/yojo/SKILL.md` |
| `cursor` | `.cursor/rules/yojo.mdc` |
| `copilot` | `.github/copilot-instructions.md` |
| `gemini` | `GEMINI.md` |

Only the section between the `yojo:begin` and `yojo:end` marker lines belongs to `yojo`; the rest
of each file is kept as it is. Running the command again rewrites nothing unless `yojo` changed.
For any other chat or tool, `yojo agent print` prints the same text to paste.

## Setup without a terminal app

Every setup step has a plain command, for scripts, CI, and agents:

| Step | Command |
|------|---------|
| Connect to each instance | `yojo init --jira-url URL --confluence-url URL --bitbucket-url URL` |
| Log in (a person) | `yojo login --service jira --base-url URL` |
| Choose projects and repositories | `yojo init ... --project KEY --repo PROJECT/slug` |
| Map fields and rules | `yojo init ... --policy-input policy.json` |
| Review and save | `yojo init ...` (preview), then the same with `--save` |
| Connect your agent | `yojo agent install --target agents` |
| Try it | `yojo --json jira search --jql 'project = PROJ' --limit 5` |

Run the login step before `init`: `init` uses the stored logins and stops with the login command
to run when one is missing.

Next:

- [Jira workflows](guides/jira-workflows.md): create, update, link, transitions, attachments
- [Confluence pages](guides/confluence.md): read, search, create, update
- [Bitbucket pull requests](guides/bitbucket.md): repositories, branches, pull requests, and diffs
- [Agent guide](agents.md): let an AI coding tool drive `yojo` safely
- [Command reference](reference/commands.md): every command, schema, and example
