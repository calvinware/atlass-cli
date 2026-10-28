"""Record the README terminal demo with VHS, against the test fixture Jira.

The tape in docs/demos/ types a literal `yojo` command. A `yojo` shim on PATH, in a temporary
folder, runs scripts/demo_fixture.py: the real parser, handlers, and renderer, with the fixture
server and an in-memory keyring. No network, no OS keyring, no real token.

VHS writes the animated GIF and a text transcript of the frames. The transcript is the stable,
reviewable record; the manifest pins the hashes of the tape, transcript, and GIF together with
the GIF's length and size, so a changed tape, fixture, or CLI output, or an edited committed file,
is caught. `--check` also decodes the last frame of a fresh recording and of the committed GIF and
compares them cell by cell (see FRAME_CELL and FRAME_TOLERANCE), so a committed GIF that no longer
shows what the tape records fails even if its manifest was regenerated to match. Font rendering and
frame timing differ slightly between machines, so the comparison is approximate: a person still
reviews the GIF before it is committed.

    python scripts/capture_demos.py --write          # regenerate docs/assets/demos/*
    python scripts/capture_demos.py --check          # re-record and fail on any drift (needs VHS)
    python scripts/capture_demos.py --check-static   # check committed files only (no VHS)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.demo_fixture import DEMO_ARGV, DEMO_COMMAND, EXPECTED_REQUESTS  # noqa: E402
from tests.conftest import BITBUCKET_TOKEN, CONFLUENCE_TOKEN, JIRA_TOKEN  # noqa: E402

PINNED_VHS = "0.12.1"
NAME = "one-call"
TAPE = ROOT / "docs" / "demos" / f"{NAME}.tape"
OUT = ROOT / "docs" / "assets" / "demos"
GIF = OUT / f"{NAME}.gif"
TRANSCRIPT = OUT / f"{NAME}.txt"
MANIFEST = OUT / f"{NAME}.json"
# Budgets for the README clip: short enough to watch before scrolling, small enough to load fast.
MAX_SECONDS = 15.0
MAX_BYTES = 1_500_000
# A re-recorded GIF may differ in bytes across machines, but not in length.
DURATION_TOLERANCE = 1.5
# The last frames are compared as the mean color of each FRAME_CELL x FRAME_CELL pixel cell. A cell
# differs when any channel's mean moves by more than FRAME_TOLERANCE (of 255); font antialiasing
# moves a cell's mean by a few units, a changed color, word, or layout by far more.
FRAME_CELL = 12
FRAME_TOLERANCE = 24
VHS_TIMEOUT = 180
COMMAND_LINE = DEMO_COMMAND
EXPECTED_TEXT = ("Created PROJ-128", "Open: https://jira.example.test/jira/browse/PROJ-128")
URL_RE = re.compile(r"https?://([^/\s\"'`]+)")
SAFE_HOST_SUFFIX = ".example.test"
TOKENS = (JIRA_TOKEN, CONFLUENCE_TOKEN, BITBUCKET_TOKEN)
FIX = "Run `python scripts/capture_demos.py --write` and review the GIF and transcript."


class DemoError(Exception):
    """Recording or checking failed; the message says what to do."""


@dataclass(frozen=True)
class Recording:
    gif: bytes
    transcript: str
    requests: list[list[str]]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tape_sha256(tape: bytes) -> str:
    """Hash the tape with LF line endings, so a CRLF checkout pins the same tape."""
    return sha256(tape.replace(b"\r\n", b"\n"))


# --------------------------------------------------------------------------- GIF


def gif_timing(data: bytes) -> tuple[int, float]:
    """Return the frame count and total length in seconds, from the GIF's own frame delays."""
    if data[:6] not in (b"GIF87a", b"GIF89a") or len(data) < 13:
        raise DemoError("not a GIF file")
    pos = 13
    flags = data[10]
    if flags & 0x80:
        pos += 3 * (2 ** ((flags & 0x07) + 1))
    frames, centiseconds = 0, 0

    def skip_sub_blocks(at: int) -> int:
        while True:
            if at >= len(data):
                raise DemoError("truncated GIF")
            size = data[at]
            at += 1
            if size == 0:
                return at
            at += size

    while pos < len(data):
        block = data[pos]
        if block == 0x3B:
            return frames, centiseconds / 100
        if block == 0x21:
            label = data[pos + 1]
            if label == 0xF9 and data[pos + 2] == 4:
                centiseconds += int.from_bytes(data[pos + 4 : pos + 6], "little")
            pos = skip_sub_blocks(pos + 2)
        elif block == 0x2C:
            frames += 1
            packed = data[pos + 9]
            pos += 10
            if packed & 0x80:
                pos += 3 * (2 ** ((packed & 0x07) + 1))
            pos = skip_sub_blocks(pos + 1)
        else:
            raise DemoError(f"unexpected GIF block 0x{block:02x}")
    raise DemoError("GIF has no trailer")


def _lzw_decode(data: bytes, min_size: int, count: int) -> bytes:
    if not 2 <= min_size <= 8:
        raise DemoError("bad GIF LZW code size")
    clear, end = 1 << min_size, (1 << min_size) + 1
    base = [bytes([i]) for i in range(clear)] + [b"", b""]
    table, size, prev = list(base), min_size + 1, b""
    out = bytearray()
    bits = nbits = 0
    for byte in data:
        bits |= byte << nbits
        nbits += 8
        while nbits >= size:
            code = bits & ((1 << size) - 1)
            bits >>= size
            nbits -= size
            if code == clear:
                table, size, prev = list(base), min_size + 1, b""
                continue
            if code == end:
                return bytes(out[:count])
            if code < len(table):
                entry = table[code]
                if prev:
                    table.append(prev + entry[:1])
            elif code == len(table) and prev:
                entry = prev + prev[:1]
                table.append(entry)
            else:
                raise DemoError("bad GIF LZW code")
            out += entry
            prev = entry
            if len(table) == 1 << size and size < 12:
                size += 1
            if len(table) > 4096:
                del table[4096:]
    return bytes(out[:count])


def _color_table(data: bytes, at: int, flags: int) -> tuple[list[int], int]:
    if not flags & 0x80:
        return [], at
    size = 2 ** ((flags & 0x07) + 1)
    raw = data[at : at + 3 * size]
    return [int.from_bytes(raw[i : i + 3], "big") for i in range(0, len(raw), 3)], at + 3 * size


def gif_last_frame(data: bytes) -> tuple[int, int, list[int]]:
    """Return the width, height, and 0xRRGGBB pixels of the GIF's last frame as it is shown."""
    if data[:6] not in (b"GIF87a", b"GIF89a") or len(data) < 13:
        raise DemoError("not a GIF file")
    width = int.from_bytes(data[6:8], "little")
    height = int.from_bytes(data[8:10], "little")
    palette, pos = _color_table(data, 13, data[10])
    background = palette[data[11]] if data[11] < len(palette) else 0
    canvas = [background] * (width * height)
    disposal, transparent = 0, None
    previous: tuple[int, int, int, int, int, list[int]] | None = None

    def sub_blocks(at: int) -> tuple[bytes, int]:
        chunks = bytearray()
        while True:
            if at >= len(data):
                raise DemoError("truncated GIF")
            size = data[at]
            if size == 0:
                return bytes(chunks), at + 1
            chunks += data[at + 1 : at + 1 + size]
            at += 1 + size

    while pos < len(data):
        block = data[pos]
        if block == 0x3B:
            return width, height, canvas
        if block == 0x21:
            if data[pos + 1] == 0xF9 and data[pos + 2] == 4:
                packed = data[pos + 3]
                disposal = (packed >> 2) & 0x07
                transparent = data[pos + 6] if packed & 0x01 else None
            _, pos = sub_blocks(pos + 2)
            continue
        if block != 0x2C:
            raise DemoError(f"unexpected GIF block 0x{block:02x}")
        if previous is not None:
            left, top, w, h, how, saved = previous
            if how == 2:
                for y in range(top, min(top + h, height)):
                    row = y * width
                    for x in range(left, min(left + w, width)):
                        canvas[row + x] = background
            elif how == 3:
                canvas = saved
        left = int.from_bytes(data[pos + 1 : pos + 3], "little")
        top = int.from_bytes(data[pos + 3 : pos + 5], "little")
        w = int.from_bytes(data[pos + 5 : pos + 7], "little")
        h = int.from_bytes(data[pos + 7 : pos + 9], "little")
        packed = data[pos + 9]
        colors, pos = _color_table(data, pos + 10, packed)
        colors = colors or palette
        min_size = data[pos]
        compressed, pos = sub_blocks(pos + 1)
        indexes = _lzw_decode(compressed, min_size, w * h)
        previous = (left, top, w, h, disposal, list(canvas) if disposal == 3 else [])
        rows = list(range(h))
        if packed & 0x40:
            rows = [*range(0, h, 8), *range(4, h, 8), *range(2, h, 4), *range(1, h, 2)]
        for n, y in enumerate(rows):
            if top + y >= height:
                continue
            line = indexes[n * w : (n + 1) * w]
            row = (top + y) * width
            for x, index in enumerate(line):
                if index != transparent and left + x < width and index < len(colors):
                    canvas[row + left + x] = colors[index]
        disposal, transparent = 0, None
    raise DemoError("GIF has no trailer")


def frame_cells(gif: bytes) -> tuple[int, int, list[tuple[int, int, int]]]:
    """Return the last frame as the mean (r, g, b) of each FRAME_CELL-pixel cell, row by row."""
    width, height, pixels = gif_last_frame(gif)
    columns, rows = -(-width // FRAME_CELL), -(-height // FRAME_CELL)
    cells = []
    for cy in range(rows):
        for cx in range(columns):
            r = g = b = n = 0
            for y in range(cy * FRAME_CELL, min((cy + 1) * FRAME_CELL, height)):
                row = y * width
                for x in range(cx * FRAME_CELL, min((cx + 1) * FRAME_CELL, width)):
                    pixel = pixels[row + x]
                    r, g, b, n = (
                        r + (pixel >> 16),
                        g + ((pixel >> 8) & 0xFF),
                        b + (pixel & 0xFF),
                        n + 1,
                    )
            cells.append((r // n, g // n, b // n))
    return columns, rows, cells


def frame_problems(fresh: bytes, committed: bytes) -> list[str]:
    """Compare the last frame of a fresh recording with the committed GIF's last frame."""
    try:
        new, old = frame_cells(fresh), frame_cells(committed)
    except DemoError as exc:
        return [f"could not compare the last frames: {exc}"]
    if new[:2] != old[:2]:
        return [f"{GIF.name}: the last frame is a different size from a fresh recording"]
    changed = sum(
        1
        for a, b in zip(new[2], old[2], strict=True)
        if max(abs(x - y) for x, y in zip(a, b, strict=True)) > FRAME_TOLERANCE
    )
    if changed:
        return [f"{GIF.name}: {changed} cell(s) of the last frame differ from a fresh recording"]
    return []


# --------------------------------------------------------------------------- checks


def transcript_problems(text: str, private: list[str]) -> list[str]:
    problems = []
    if "".join(COMMAND_LINE.split()) not in "".join(text.split()):
        # A command wider than the terminal wraps across rows; it must still read exactly.
        problems.append("the transcript does not show the literal demo command")
    for expected in EXPECTED_TEXT:
        if expected not in text:
            problems.append(f"the transcript does not show {expected!r}")
    for token in TOKENS:
        if token in text:
            problems.append("the transcript shows a fixture token")
    for value in private:
        if value and value in text:
            problems.append(f"the transcript shows a local path ({value})")
    for host in URL_RE.findall(text):
        if not host.endswith(SAFE_HOST_SUFFIX):
            problems.append(f"the transcript shows host {host!r}, not a *{SAFE_HOST_SUFFIX} host")
    return problems


def gif_problems(gif: bytes) -> list[str]:
    try:
        frames, seconds = gif_timing(gif)
    except DemoError as exc:
        return [f"{GIF.name}: {exc}"]
    problems = []
    if frames < 2:
        problems.append(f"{GIF.name} has {frames} frame(s); expected an animation")
    if seconds > MAX_SECONDS:
        problems.append(f"{GIF.name} runs {seconds:.2f} s, over the {MAX_SECONDS:.0f} s budget")
    if len(gif) > MAX_BYTES:
        problems.append(f"{GIF.name} is {len(gif)} bytes, over the {MAX_BYTES} byte budget")
    return problems


def manifest_for(tape: bytes, recording: Recording) -> dict[str, Any]:
    frames, seconds = gif_timing(recording.gif)
    return {
        "vhs": PINNED_VHS,
        "command": COMMAND_LINE,
        "tape_sha256": tape_sha256(tape),
        "transcript_sha256": sha256(recording.transcript.encode("utf-8")),
        "gif_sha256": sha256(recording.gif),
        "gif_bytes": len(recording.gif),
        "gif_frames": frames,
        "duration_seconds": seconds,
        "requests": recording.requests,
    }


def private_values() -> list[str]:
    return [str(ROOT), str(Path.home()), tempfile.gettempdir()]


def static_problems() -> list[str]:
    """Check the committed tape, transcript, GIF, and manifest against each other (no VHS)."""
    missing = [p.relative_to(ROOT).as_posix() for p in (TAPE, GIF, TRANSCRIPT, MANIFEST)]
    missing = [
        m
        for m, p in zip(missing, (TAPE, GIF, TRANSCRIPT, MANIFEST), strict=True)
        if not p.is_file()
    ]
    if missing:
        return [f"missing demo files: {', '.join(missing)}"]
    tape = TAPE.read_bytes()
    gif = GIF.read_bytes()
    transcript = TRANSCRIPT.read_text(encoding="utf-8")
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f"{MANIFEST.name} is not JSON: {exc}"]
    if not isinstance(manifest, dict):
        return [f"{MANIFEST.name} must be a JSON object"]
    problems = gif_problems(gif) + transcript_problems(transcript, private_values())
    if shlex.split(COMMAND_LINE)[1:] != list(DEMO_ARGV):
        problems.append("the typed demo command does not parse to the argv the shim runs")
    if not problems:
        expected = manifest_for(tape, Recording(gif, transcript, EXPECTED_REQUESTS))
        for key, value in expected.items():
            if manifest.get(key) != value:
                problems.append(f"{MANIFEST.name}: {key} does not match the committed files")
        if set(manifest) != set(expected):
            problems.append(
                f"{MANIFEST.name}: unexpected keys {sorted(set(manifest) - set(expected))}"
            )
    return problems


# --------------------------------------------------------------------------- recording


def vhs_version(vhs: str) -> str:
    proc = subprocess.run(  # noqa: S603 - fixed argv, the VHS found on PATH
        [vhs, "--version"], capture_output=True, text=True, timeout=30, check=False
    )
    match = re.search(r"(\d+\.\d+\.\d+)", proc.stdout + proc.stderr)
    return match.group(1) if match else "unknown"


def find_vhs() -> str:
    vhs = shutil.which("vhs")
    if vhs is None:
        raise DemoError(
            f"VHS is not installed. Install VHS {PINNED_VHS} (with ttyd and ffmpeg), for example "
            "`brew install vhs` or the release from https://github.com/charmbracelet/vhs/releases."
        )
    version = vhs_version(vhs)
    if version != PINNED_VHS:
        raise DemoError(f"VHS {version} is installed; the demo is pinned to VHS {PINNED_VHS}.")
    return vhs


def record(vhs: str) -> Recording:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp).resolve()
        bin_dir = work / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "yojo"
        shim.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} "
            f'{shlex.quote(str(ROOT / "scripts" / "demo_fixture.py"))} "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)
        log = work / "requests.json"
        tape = work / f"{NAME}.tape"
        tape.write_text(
            TAPE.read_text(encoding="utf-8") + f"\nOutput {NAME}.gif\nOutput {NAME}.txt\n",
            encoding="utf-8",
        )
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "YOJO_DEMO_REQUEST_LOG": str(log),
        }
        env.pop("NO_COLOR", None)
        proc = subprocess.run(  # noqa: S603 - fixed argv: pinned VHS and a generated tape
            [vhs, tape.name],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=VHS_TIMEOUT,
            check=False,
        )
        gif, text = work / f"{NAME}.gif", work / f"{NAME}.txt"
        if proc.returncode != 0 or not gif.is_file() or not text.is_file():
            raise DemoError(f"VHS failed (exit {proc.returncode}):\n{proc.stdout}{proc.stderr}")
        if not log.is_file():
            raise DemoError("the demo never ran the fixture `yojo`; check the tape's command")
        transcript = normalize(text.read_text(encoding="utf-8"))
        requests = json.loads(log.read_text(encoding="utf-8"))
        private = [*private_values(), str(work)]
        problems = transcript_problems(transcript, private)
        if requests != EXPECTED_REQUESTS:
            problems.append(
                f"the demo sent {requests}; expected the preflight reads, one POST, and the "
                f"confirming read: {EXPECTED_REQUESTS}"
            )
        recording = Recording(gif.read_bytes(), transcript, requests)
        problems += gif_problems(recording.gif)
        if problems:
            raise DemoError("\n".join(problems))
        return recording


def normalize(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    return "\n".join(lines).strip("\n") + "\n"


def check_recording(recording: Recording) -> list[str]:
    problems = static_problems()
    committed = TRANSCRIPT.read_text(encoding="utf-8") if TRANSCRIPT.is_file() else ""
    if recording.transcript != committed:
        problems.append(f"{TRANSCRIPT.name} differs from a fresh recording")
    if GIF.is_file():
        problems += frame_problems(recording.gif, GIF.read_bytes())
    if MANIFEST.is_file():
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        _, seconds = gif_timing(recording.gif)
        recorded = manifest.get("duration_seconds")
        if not isinstance(recorded, int | float) or abs(seconds - recorded) > DURATION_TOLERANCE:
            problems.append(
                f"a fresh recording runs {seconds:.2f} s, the committed GIF {recorded} s"
            )
    return problems


def write(recording: Recording) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    GIF.write_bytes(recording.gif)
    TRANSCRIPT.write_text(recording.transcript, encoding="utf-8", newline="\n")
    manifest = manifest_for(TAPE.read_bytes(), recording)
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    for path in (GIF, TRANSCRIPT, MANIFEST):
        sys.stdout.write(f"wrote {path.relative_to(ROOT).as_posix()}\n")
    sys.stdout.write(
        f"{manifest['duration_seconds']:.2f} s, {manifest['gif_bytes']} bytes, "
        f"{manifest['gif_frames']} frames\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="re-record and write the demo files")
    mode.add_argument("--check", action="store_true", help="re-record and fail on any drift")
    mode.add_argument(
        "--check-static", action="store_true", help="check the committed files only, without VHS"
    )
    args = parser.parse_args(argv)
    try:
        if args.check_static:
            problems = static_problems()
        else:
            recording = record(find_vhs())
            if args.write:
                write(recording)
                return 0
            problems = check_recording(recording)
    except DemoError as exc:
        sys.stderr.write(f"demo capture failed:\n{exc}\n")
        return 2
    if problems:
        sys.stderr.write("Stale terminal demo:\n" + "\n".join(f"- {p}" for p in problems))
        sys.stderr.write(f"\n{FIX}\n")
        return 1
    sys.stdout.write("terminal demo is up to date\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
