from __future__ import annotations

import io
import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

from scripts import capture_demos as demos
from scripts import demo_fixture
from tests.conftest import JIRA_TOKEN


def run(argv: tuple[str, ...] | list[str], *, tty: bool) -> tuple[int, str, str, list[list[str]]]:
    stdout, stderr = io.StringIO(), io.StringIO()
    code, server = demo_fixture.run_demo(
        argv, stdout, stderr, stdout_isatty=tty, environ={"NO_COLOR": "1", "HOME": "/x"}
    )
    return code, stdout.getvalue(), stderr.getvalue(), demo_fixture.request_log(server)


def test_demo_creates_and_confirms_in_one_post() -> None:
    code, out, err, requests = run(demo_fixture.DEMO_ARGV, tty=True)
    assert code == 0, out
    assert out.startswith("Created PROJ-128\n")
    assert "Open: https://jira.example.test/jira/browse/PROJ-128" in out
    assert "Sending:" in err
    assert requests == demo_fixture.EXPECTED_REQUESTS
    assert [m for m, _ in requests].count("POST") == 1
    assert JIRA_TOKEN not in out + err


def test_demo_json_face_is_the_same_result() -> None:
    code, out, _, requests = run(demo_fixture.DEMO_ARGV, tty=False)
    envelope = json.loads(out)
    assert code == 0
    assert envelope["data"]["key"] == "PROJ-128"
    assert envelope["data"]["url"] == "https://jira.example.test/jira/browse/PROJ-128"
    assert requests == demo_fixture.EXPECTED_REQUESTS


@pytest.mark.parametrize(
    "argv",
    [
        ["login", "--service", "jira"],
        ["jira", "issue", "get", "PROJ-1", "--base-url", "https://jira.example.test/jira"],
        [*demo_fixture.DEMO_ARGV, "--set", "Priority=High"],
        [],
    ],
)
def test_shim_refuses_any_other_command(argv: list[str]) -> None:
    with pytest.raises(demo_fixture.DemoRefused):
        run(argv, tty=True)
    assert demo_fixture.cli(argv) == 2


def test_tape_types_the_literal_demo_command() -> None:
    tape = demos.TAPE.read_text(encoding="utf-8")
    assert f"Type '{demos.COMMAND_LINE}'" in tape
    assert "Output" not in tape.replace("# capture_demos.py adds the Output lines", "")
    for setting in (
        "Set Shell",
        "Set FontFamily",
        "Set FontSize",
        "Set Width",
        "Set Height",
        "Set Theme",
        "Set TypingSpeed",
        "Env LANG",
    ):
        assert setting in tape, setting


def test_committed_demo_is_consistent() -> None:
    assert demos.static_problems() == []
    manifest = json.loads(demos.MANIFEST.read_text(encoding="utf-8"))
    assert manifest["vhs"] == demos.PINNED_VHS
    assert manifest["duration_seconds"] <= demos.MAX_SECONDS
    assert manifest["gif_bytes"] <= demos.MAX_BYTES
    assert manifest["requests"] == demo_fixture.EXPECTED_REQUESTS


@pytest.fixture
def copied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("TAPE", "GIF", "TRANSCRIPT", "MANIFEST"):
        source: Path = getattr(demos, name)
        target = tmp_path / source.name
        shutil.copyfile(source, target)
        monkeypatch.setattr(demos, name, target)
    monkeypatch.setattr(demos, "ROOT", tmp_path)
    return tmp_path


def test_changed_tape_is_stale(copied: Path) -> None:
    demos.TAPE.write_text(demos.TAPE.read_text(encoding="utf-8") + "Sleep 1s\n", encoding="utf-8")
    assert any("tape_sha256" in p for p in demos.static_problems())


def test_changed_transcript_is_stale(copied: Path) -> None:
    text = demos.TRANSCRIPT.read_text(encoding="utf-8")
    demos.TRANSCRIPT.write_text(text + "extra\n", encoding="utf-8")
    assert any("transcript_sha256" in p for p in demos.static_problems())


def test_changed_gif_is_stale(copied: Path) -> None:
    data = bytearray(demos.GIF.read_bytes())
    data[20] ^= 0xFF  # a global color table byte: still a valid GIF
    demos.GIF.write_bytes(bytes(data))
    assert any("gif_sha256" in p for p in demos.static_problems())


def test_crlf_tape_checkout_has_the_same_hash(copied: Path) -> None:
    lf = demos.TAPE.read_bytes().replace(b"\r\n", b"\n")
    demos.TAPE.write_bytes(lf.replace(b"\n", b"\r\n"))
    assert demos.static_problems() == []
    assert demos.tape_sha256(lf) == demos.tape_sha256(lf.replace(b"\n", b"\r\n"))


def test_fresh_frame_matches_committed_frame() -> None:
    gif = demos.GIF.read_bytes()
    assert demos.frame_problems(gif, gif) == []
    columns, rows, cells = demos.frame_cells(gif)
    assert len(cells) == columns * rows > 1


def test_changed_frame_is_caught_even_with_a_regenerated_manifest(copied: Path) -> None:
    fresh = demos.GIF.read_bytes()
    _, _, pixels = demos.gif_last_frame(fresh)
    palette, _ = demos._color_table(fresh, 13, fresh[10])
    background = palette.index(Counter(pixels).most_common(1)[0][0])
    changed = bytearray(fresh)
    changed[13 + 3 * background + 1] ^= 0xFF  # still a valid GIF, visibly a different color
    demos.GIF.write_bytes(bytes(changed))
    transcript = demos.TRANSCRIPT.read_text(encoding="utf-8")
    recording = demos.Recording(fresh, transcript, demo_fixture.EXPECTED_REQUESTS)
    manifest = demos.manifest_for(
        demos.TAPE.read_bytes(),
        demos.Recording(bytes(changed), transcript, demo_fixture.EXPECTED_REQUESTS),
    )
    demos.MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    assert demos.static_problems() == []
    problems = demos.check_recording(recording)
    assert len(problems) == 1
    assert "cell(s) of the last frame differ" in problems[0]


def test_missing_gif_is_reported(copied: Path) -> None:
    demos.GIF.unlink()
    assert any("missing demo files" in p for p in demos.static_problems())


@pytest.mark.parametrize(
    ("addition", "expected"),
    [
        (f"token {JIRA_TOKEN}\n", "fixture token"),
        ("see https://jira.corp.invalid/browse/X\n", "not a *.example.test host"),
    ],
)
def test_transcript_leaks_are_reported(copied: Path, addition: str, expected: str) -> None:
    text = demos.TRANSCRIPT.read_text(encoding="utf-8")
    demos.TRANSCRIPT.write_text(text + addition, encoding="utf-8")
    assert any(expected in p for p in demos.static_problems())


def test_local_paths_in_transcript_are_reported() -> None:
    text = f"{demos.COMMAND_LINE}\n{demos.EXPECTED_TEXT[0]}\n{demos.EXPECTED_TEXT[1]}\n"
    assert demos.transcript_problems(text, ["/home/someone"]) == []
    assert any(
        "local path" in p
        for p in demos.transcript_problems(text + "/home/someone/x\n", ["/home/someone"])
    )


def test_budgets_are_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    gif = demos.GIF.read_bytes()
    assert demos.gif_problems(gif) == []
    monkeypatch.setattr(demos, "MAX_SECONDS", 1.0)
    monkeypatch.setattr(demos, "MAX_BYTES", 1000)
    problems = demos.gif_problems(gif)
    assert any("s budget" in p for p in problems)
    assert any("byte budget" in p for p in problems)


def test_gif_timing_reads_frame_delays() -> None:
    frames, seconds = demos.gif_timing(demos.GIF.read_bytes())
    assert frames > 1
    assert 3.0 < seconds <= demos.MAX_SECONDS
    with pytest.raises(demos.DemoError):
        demos.gif_timing(b"not a gif")


def test_static_check_cli(copied: Path) -> None:
    assert demos.main(["--check-static"]) == 0
    demos.TRANSCRIPT.write_text("changed\n", encoding="utf-8")
    assert demos.main(["--check-static"]) == 1
