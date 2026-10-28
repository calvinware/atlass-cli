"""Check the built wheel and sdist: identity, core metadata, and the complete MIT license.

Both archives must be the `yojo` distribution at the pyproject.toml version, and the wheel must
install only the `yojo` package and the `yojo` command (no leftover `atlass_cli` module or `atl`
entry point). The source LICENSE must contain the copyright line, permission grant, condition,
and disclaimer. Each archive's LICENSE must be byte-identical to that validated source file.

    python scripts/check_package.py dist   # exit 1 and list every problem on failure
"""

from __future__ import annotations

import argparse
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LICENSE_PATH = ROOT / "LICENSE"
PYPROJECT_PATH = ROOT / "pyproject.toml"
DIST_NAME = "yojo"
ENTRY_POINT = "yojo = yojo.cli:main"
LEGACY_PACKAGE = "atlass_cli"
LEGACY_COMMAND = "atl"

LICENSE_CLAUSES = {
    "title": "MIT License",
    "copyright": "Copyright (c) 2026 Calvinware",
    "permission grant": (
        "Permission is hereby granted, free of charge, to any person obtaining a copy of this "
        'software and associated documentation files (the "Software"), to deal in the Software '
        "without restriction, including without limitation the rights to use, copy, modify, "
        "merge, publish, distribute, sublicense, and/or sell copies of the Software, and to "
        "permit persons to whom the Software is furnished to do so, subject to the following "
        "conditions:"
    ),
    "condition": (
        "The above copyright notice and this permission notice shall be included in all copies "
        "or substantial portions of the Software."
    ),
    "disclaimer": (
        'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, '
        "INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A "
        "PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT "
        "HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF "
        "CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE "
        "OR THE USE OR OTHER DEALINGS IN THE SOFTWARE."
    ),
}
EXPECTED_METADATA = (
    "License-Expression: MIT",
    "License-File: LICENSE",
    "Requires-Python: >=3.11",
    "Classifier: Programming Language :: Python :: 3.14",
    "Classifier: Programming Language :: Python :: 3.15",
)


def project_version(pyproject: Path = PYPROJECT_PATH) -> str:
    with pyproject.open("rb") as f:
        return str(tomllib.load(f)["project"]["version"])


def license_problems(text: str) -> list[str]:
    normalized = " ".join(text.split())
    return [
        f"LICENSE is missing the MIT {name}"
        for name, clause in LICENSE_CLAUSES.items()
        if clause not in normalized
    ]


def metadata_problems(label: str, metadata: str, version: str) -> list[str]:
    lines = metadata.split("\n\n", 1)[0].splitlines()
    expected = (f"Name: {DIST_NAME}", f"Version: {version}", *EXPECTED_METADATA)
    missing = [field for field in expected if field not in lines]
    problems = [f"{label} metadata is missing {field!r}" for field in missing]
    if any(line.startswith("License: ") for line in lines):
        problems.append(f"{label} metadata has a legacy License field")
    return problems


def _one(label: str, names: list[str]) -> str:
    if len(names) != 1:
        raise ValueError(f"expected exactly one {label}, found {len(names)}: {names}")
    return names[0]


def _read_wheel(path: Path) -> tuple[str, bytes, list[str], str]:
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        meta = _one("wheel METADATA", [n for n in names if n.endswith(".dist-info/METADATA")])
        lic = _one("wheel LICENSE", [n for n in names if n.endswith(".dist-info/licenses/LICENSE")])
        entry = _one(
            "wheel entry_points.txt",
            [n for n in names if n.endswith(".dist-info/entry_points.txt")],
        )
        return (
            zf.read(meta).decode("utf-8"),
            zf.read(lic),
            names,
            zf.read(entry).decode("utf-8"),
        )


def wheel_content_problems(names: list[str], entry_points: str) -> list[str]:
    problems = []
    if not any(n.startswith(f"{DIST_NAME}/") for n in names):
        problems.append(f"wheel has no {DIST_NAME}/ package")
    if any(n.split("/", 1)[0] == LEGACY_PACKAGE for n in names):
        problems.append(f"wheel still contains the {LEGACY_PACKAGE} package")
    scripts: list[str] = []
    section = ""
    for raw in entry_points.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line
        elif line and section == "[console_scripts]":
            scripts.append(" ".join(line.replace("=", " = ", 1).split()))
    if ENTRY_POINT not in scripts:
        problems.append(f"wheel console_scripts is missing {ENTRY_POINT!r}")
    if any(s.split(" = ", 1)[0] == LEGACY_COMMAND for s in scripts):
        problems.append(f"wheel still installs the {LEGACY_COMMAND!r} command")
    return problems


def _read_sdist(path: Path) -> tuple[str, bytes]:
    with tarfile.open(path) as tf:
        top = [m.name for m in tf.getmembers() if m.isfile() and m.name.count("/") == 1]
        meta = tf.getmember(_one("sdist PKG-INFO", [n for n in top if n.endswith("/PKG-INFO")]))
        lic = tf.getmember(_one("sdist LICENSE", [n for n in top if n.endswith("/LICENSE")]))
        meta_file, lic_file = tf.extractfile(meta), tf.extractfile(lic)
        if meta_file is None or lic_file is None:
            raise ValueError("sdist PKG-INFO or LICENSE is not a regular file")
        return meta_file.read().decode("utf-8"), lic_file.read()


def check(
    dist: Path, license_path: Path = LICENSE_PATH, pyproject: Path = PYPROJECT_PATH
) -> list[str]:
    source = license_path.read_bytes()
    problems = license_problems(source.decode("utf-8"))
    version = project_version(pyproject)
    try:
        wheel = _one("wheel", sorted(str(p) for p in dist.glob("*.whl")))
        sdist = _one("sdist", sorted(str(p) for p in dist.glob("*.tar.gz")))
        wheel_meta, wheel_license, names, entry_points = _read_wheel(Path(wheel))
        archives = (("wheel", (wheel_meta, wheel_license)), ("sdist", _read_sdist(Path(sdist))))
    except (ValueError, KeyError, OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
        return [*problems, str(exc)]
    problems += wheel_content_problems(names, entry_points)
    for label, (metadata, license_bytes) in archives:
        problems += metadata_problems(label, metadata, version)
        if license_bytes != source:
            problems.append(f"{label} LICENSE differs from the source LICENSE")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("dist", type=Path, help="directory holding one wheel and one sdist")
    args = parser.parse_args(argv)
    problems = check(args.dist)
    if problems:
        sys.stderr.write("".join(f"::error::{p}\n" for p in problems))
        return 1
    sys.stdout.write("package identity, metadata, and license ok\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
