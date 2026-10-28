"""When `yojo setup` and bare `yojo` open the app, and the one JSON envelope when they do not."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import Harness
from yojo.cli import Runtime, main
from yojo.contracts import ExitCode

SRC = str(Path(__file__).resolve().parents[1] / "src")
SUMMARY = {
    "status": "cancelled",
    "config": None,
    "logins_stored": [],
    "agent_files": [],
    "notes": ["yojo.yaml was not changed."],
    "next": ["yojo setup"],
}


def needs_terminal_envelope() -> dict[str, Any]:
    from yojo.cli import _setup_needs_terminal

    return _setup_needs_terminal().envelope()


def launcher(calls: list[Runtime]) -> Any:
    def launch(rt: Runtime) -> dict[str, Any]:
        calls.append(rt)
        return dict(SUMMARY)

    return launch


def run(
    harness: Harness,
    *argv: str,
    stdout_tty: bool,
    stdin_tty: bool,
    environ: dict[str, str] | None = None,
) -> tuple[int, str, list[Runtime]]:
    calls: list[Runtime] = []
    rt, stdout, _ = harness.runtime(
        b"", stdin_tty, None, None, stdout_tty=stdout_tty, environ=environ
    )
    rt.launch_setup = launcher(calls)
    code = main(list(argv), rt)
    return code, stdout.getvalue(), calls


@pytest.mark.parametrize(
    ("argv", "stdout_tty", "stdin_tty", "environ"),
    [
        (["setup"], False, True, {}),
        (["setup"], False, False, {}),
        (["--json", "setup"], True, True, {}),
        (["setup", "--json"], True, True, {}),
        (["setup"], True, False, {}),
        (["setup"], True, True, {"TERM": "dumb"}),
    ],
)
def test_setup_without_a_usable_terminal_is_one_usage_envelope(
    harness: Harness,
    argv: list[str],
    stdout_tty: bool,
    stdin_tty: bool,
    environ: dict[str, str],
) -> None:
    code, out, calls = run(
        harness, *argv, stdout_tty=stdout_tty, stdin_tty=stdin_tty, environ=environ
    )
    assert code == ExitCode.INPUT
    assert calls == []
    lines = out.splitlines()
    assert len(lines) == 1
    assert lines[0] == json.dumps(needs_terminal_envelope(), separators=(",", ":"))
    envelope = json.loads(lines[0])
    assert envelope["ok"] is False
    error = envelope["error"]
    assert error["code"] == "usage_error"
    assert "yojo login" in error["fix"] and "init" in error["fix"]
    assert "agent install" in error["fix"]
    assert error["details"]["steps"][0].startswith("yojo login")
    assert harness.server.requests == []
    assert list(harness.tmp_path.iterdir()) == []


def test_setup_in_a_terminal_opens_the_app_and_renders_its_summary(harness: Harness) -> None:
    code, out, calls = run(harness, "setup", stdout_tty=True, stdin_tty=True)
    assert code == ExitCode.OK
    assert len(calls) == 1
    assert "Setup closed before the last step." in out
    assert "yojo.yaml was not changed." in out


def test_setup_refuses_service_options(harness: Harness) -> None:
    result = harness.run("setup", "--base-url", "https://jira.example.test/jira")
    assert result.code == ExitCode.INPUT
    assert result.error["code"] == "usage_error"


def test_bare_yojo_opens_setup_on_first_run_in_a_terminal(harness: Harness) -> None:
    code, _, calls = run(harness, stdout_tty=True, stdin_tty=True)
    assert code == ExitCode.OK
    assert len(calls) == 1


@pytest.mark.parametrize(("stdout_tty", "stdin_tty"), [(False, True), (True, False)])
def test_bare_yojo_off_terminal_is_unchanged_and_reads_no_config(
    harness: Harness, stdout_tty: bool, stdin_tty: bool
) -> None:
    (harness.tmp_path / "yojo.yaml").write_text("version: 99\n")
    code, _, calls = run(harness, stdout_tty=stdout_tty, stdin_tty=stdin_tty)
    assert code == ExitCode.INPUT
    assert calls == []


def test_bare_json_never_opens_setup(harness: Harness) -> None:
    code, out, calls = run(harness, "--json", stdout_tty=True, stdin_tty=True)
    assert code == ExitCode.INPUT
    assert calls == []
    assert json.loads(out)["error"]["code"] == "usage_error"


def test_configured_bare_terminal_shows_next_commands_and_changes_nothing(
    harness: Harness,
) -> None:
    config = harness.write_config()
    before = config.read_bytes()
    code, out, calls = run(harness, stdout_tty=True, stdin_tty=True)
    assert code == ExitCode.INPUT
    assert calls == []
    assert "yojo commands" in out and "yojo setup" in out
    assert config.read_bytes() == before


def test_bare_terminal_with_legacy_config_gets_the_legacy_error(harness: Harness) -> None:
    (harness.tmp_path / "atl.yaml").write_text("version: 2\n")
    code, out, calls = run(harness, stdout_tty=True, stdin_tty=True)
    assert code == ExitCode.INPUT
    assert calls == []
    assert "atl.yaml" in out


def test_setup_summary_matches_the_schema(harness: Harness) -> None:
    calls: list[Runtime] = []
    rt, _, _ = harness.runtime(b"", True, None, None, stdout_tty=True)
    rt.launch_setup = launcher(calls)
    from jsonschema import Draft202012Validator

    from yojo.cli import outcome
    from yojo.contracts import COMMANDS_BY_PATH, command_schema

    rt.json_mode = False
    envelope, code = outcome(["setup"], rt)
    assert code == 0
    Draft202012Validator(command_schema(COMMANDS_BY_PATH["setup"])["success"]).validate(envelope)


# --------------------------------------------------------------------------- real processes


def _env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("NO_COLOR", "TERM")}
    env.update({"PYTHONPATH": SRC, "HOME": str(tmp_path), "TERM": "xterm-256color"})
    return env


def test_piped_setup_prints_one_json_line_and_never_imports_textual(tmp_path: Path) -> None:
    script = (
        "import sys\n"
        "from yojo.cli import main\n"
        "code = main(sys.argv[1:])\n"
        "sys.stderr.write('textual-loaded' if 'textual' in sys.modules else 'textual-absent')\n"
        "raise SystemExit(code)\n"
    )
    for argv in (["setup"], [], ["--json", "setup"]):
        done = subprocess.run(  # noqa: S603 - fixed interpreter and arguments
            [sys.executable, "-c", script, *argv],
            cwd=tmp_path,
            env=_env(tmp_path),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert done.returncode == 2, done
        lines = done.stdout.decode().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["error"]["code"] == "usage_error"
        assert done.stderr.decode().endswith("textual-absent")


@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX pseudo-terminal")
@pytest.mark.parametrize(("stdin_closed", "term"), [(True, "xterm-256color"), (False, "dumb")])
def test_unusable_terminal_prints_one_json_line_and_never_imports_textual(
    tmp_path: Path, stdin_closed: bool, term: str
) -> None:
    """stdout is a terminal, but stdin is not one or TERM=dumb: the app cannot run there."""
    pty = pytest.importorskip("pty")
    import select

    script = (
        "import sys\n"
        "from yojo.cli import main\n"
        "code = main(['setup'])\n"
        "sys.stderr.write('textual-loaded' if 'textual' in sys.modules else 'textual-absent')\n"
        "raise SystemExit(code)\n"
    )
    env = _env(tmp_path)
    env["TERM"] = term
    parent, child = pty.openpty()
    stdin_parent, stdin_child = pty.openpty()
    try:
        done = subprocess.run(  # noqa: S603 - fixed interpreter and arguments
            [sys.executable, "-c", script],
            cwd=tmp_path,
            env=env,
            stdin=subprocess.DEVNULL if stdin_closed else stdin_child,
            stdout=child,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        # The terminal stays open here while it is drained: some systems drop unread output
        # once every writer has closed it.
        output = b""
        while select.select([parent], [], [], 0.5)[0]:
            chunk = os.read(parent, 65536)
            if not chunk:
                break
            output += chunk
    finally:
        for fd in (parent, child, stdin_parent, stdin_child):
            os.close(fd)
    assert done.returncode == 2, done
    lines = output.decode().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == needs_terminal_envelope()
    assert done.stderr.decode().endswith("textual-absent")
    assert list(tmp_path.iterdir()) == []


def _pty_session(
    tmp_path: Path, argv: list[str], keys: list[bytes], wait_for: bytes
) -> tuple[int, bytes]:
    """Run `python -m yojo ARGV` with stdin and stdout on a pseudo-terminal, typing ``keys``."""
    # POSIX-only modules, loaded so that type checks on Windows see no platform-specific names.
    pty = pytest.importorskip("pty")
    fcntl = pytest.importorskip("fcntl")
    termios = pytest.importorskip("termios")
    import select
    import struct

    fd, child_end = pty.openpty()
    fcntl.ioctl(child_end, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    proc = subprocess.Popen(  # noqa: S603 - fixed interpreter and arguments
        [sys.executable, "-m", "yojo", *argv],
        stdin=child_end,
        stdout=child_end,
        stderr=child_end,
        cwd=tmp_path,
        env=_env(tmp_path),
        start_new_session=True,
    )
    os.close(child_end)
    output = b""
    deadline = time.monotonic() + 60
    sent = False
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:  # EIO: the child closed the terminal
                break
            if not chunk:
                break
            output += chunk
            if not sent and wait_for in output:
                time.sleep(0.5)
                for key in keys:
                    os.write(fd, key)
                    time.sleep(0.4)
                sent = True
        else:
            proc.kill()
            raise AssertionError(f"yojo did not finish in a terminal: {output[-2000:]!r}")
        return proc.wait(timeout=30), output
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(fd)


@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX pseudo-terminal")
@pytest.mark.parametrize("argv", [["setup"], []])
def test_terminal_opens_the_app_and_leaving_writes_nothing(tmp_path: Path, argv: list[str]) -> None:
    # Ctrl+Q asks to leave; Tab moves to "Leave setup"; Enter confirms.
    code, output = _pty_session(tmp_path, argv, [b"\x11", b"\t", b"\r"], b"Step 1 of 7")
    assert code == 0
    assert b"Setup closed before the last step." in output
    assert sorted(p.name for p in tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX pseudo-terminal")
def test_terminal_bare_yojo_with_config_does_not_open_the_app(tmp_path: Path) -> None:
    (tmp_path / "yojo.yaml").write_text("version: 1\njira:\n  base_url: https://j.example.test\n")
    code, output = _pty_session(tmp_path, [], [], b"\x00never")
    assert code == 2
    assert b"No command given." in output
    assert b"Step 1 of 7" not in output
