"""Check the built wheel and sdist: core metadata and the complete MIT license.

The source LICENSE must contain the copyright line, permission grant, condition, and disclaimer.
Each archive's LICENSE must be byte-identical to that validated source file.

    python scripts/check_package.py dist   # exit 1 and list every problem on failure
"""

from __future__ import annotations

import argparse
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LICENSE_PATH = ROOT / "LICENSE"

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


def license_problems(text: str) -> list[str]:
    normalized = " ".join(text.split())
    return [
        f"LICENSE is missing the MIT {name}"
        for name, clause in LICENSE_CLAUSES.items()
        if clause not in normalized
    ]


def metadata_problems(label: str, metadata: str) -> list[str]:
    lines = metadata.split("\n\n", 1)[0].splitlines()
    missing = [field for field in EXPECTED_METADATA if field not in lines]
    problems = [f"{label} metadata is missing {field!r}" for field in missing]
    if any(line.startswith("License: ") for line in lines):
        problems.append(f"{label} metadata has a legacy License field")
    return problems


def _one(label: str, names: list[str]) -> str:
    if len(names) != 1:
        raise ValueError(f"expected exactly one {label}, found {len(names)}: {names}")
    return names[0]


def _read_wheel(path: Path) -> tuple[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        meta = _one("wheel METADATA", [n for n in names if n.endswith(".dist-info/METADATA")])
        lic = _one("wheel LICENSE", [n for n in names if n.endswith(".dist-info/licenses/LICENSE")])
        return zf.read(meta).decode("utf-8"), zf.read(lic)


def _read_sdist(path: Path) -> tuple[str, bytes]:
    with tarfile.open(path) as tf:
        top = [m.name for m in tf.getmembers() if m.isfile() and m.name.count("/") == 1]
        meta = tf.getmember(_one("sdist PKG-INFO", [n for n in top if n.endswith("/PKG-INFO")]))
        lic = tf.getmember(_one("sdist LICENSE", [n for n in top if n.endswith("/LICENSE")]))
        meta_file, lic_file = tf.extractfile(meta), tf.extractfile(lic)
        if meta_file is None or lic_file is None:
            raise ValueError("sdist PKG-INFO or LICENSE is not a regular file")
        return meta_file.read().decode("utf-8"), lic_file.read()


def check(dist: Path, license_path: Path = LICENSE_PATH) -> list[str]:
    source = license_path.read_bytes()
    problems = license_problems(source.decode("utf-8"))
    try:
        wheel = _one("wheel", sorted(str(p) for p in dist.glob("*.whl")))
        sdist = _one("sdist", sorted(str(p) for p in dist.glob("*.tar.gz")))
        archives = (("wheel", _read_wheel(Path(wheel))), ("sdist", _read_sdist(Path(sdist))))
    except (ValueError, KeyError, OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
        return [*problems, str(exc)]
    for label, (metadata, license_bytes) in archives:
        problems += metadata_problems(label, metadata)
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
    sys.stdout.write("package metadata and license ok\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
