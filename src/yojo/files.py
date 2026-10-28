"""Conflict-checked, atomic writes of small local text files (yojo.yaml, agent instructions).

A new file is staged next to its destination, flushed to disk, and published with a hard link,
which fails if the destination appeared in the meantime. A changed file is compared with the bytes
the caller read immediately before an atomic rename. Symbolic links are never written through.
Cooperating ``yojo`` writers share a per-destination lock file. Portable filesystems do not offer
an atomic content-compare-and-replace operation: a non-``yojo`` writer that changes the file
between the final comparison and the rename is not detected. Callers must avoid concurrent editors
when replacing an existing file.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import tempfile
from pathlib import Path

from yojo.contracts import YojoError

MAX_LOCAL_BYTES = 1024 * 1024


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resolve(cwd: Path, raw: str) -> Path:
    path = Path(raw)
    return Path(os.path.normpath(path if path.is_absolute() else cwd / path))


def _invalid(path: Path, message: str, fix: str) -> YojoError:
    return YojoError("file_invalid", message, fix, {"path": str(path)})


def conflict(path: Path, message: str, fix: str, **details: object) -> YojoError:
    return YojoError("file_conflict", message, fix, {"path": str(path), **details})


def refuse_symlinks(path: Path) -> None:
    """Refuse a link at ``path`` or any ancestor."""
    for item in reversed((path, *path.parents)):
        if item.is_symlink():
            raise _invalid(
                item,
                f"{item} is a symbolic link; yojo does not write through links. Nothing was "
                "written.",
                f"Replace {item} with a regular file or directory, or choose another path.",
            )


def read_current(path: Path) -> bytes | None:
    """The file's bytes, or None when it does not exist."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise _invalid(
            path, f"Cannot inspect {path}.", "Check the path and its permissions."
        ) from None
    if stat.S_ISLNK(info.st_mode):
        refuse_symlinks(path)
    if not stat.S_ISREG(info.st_mode):
        raise _invalid(path, f"{path} is not a regular file.", "Choose a path to a regular file.")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(MAX_LOCAL_BYTES + 1)
    except OSError:
        raise _invalid(path, f"Cannot read {path}.", "Check the file's permissions.") from None
    if len(data) > MAX_LOCAL_BYTES:
        raise _invalid(
            path,
            f"{path} is larger than {MAX_LOCAL_BYTES} bytes; yojo will not rewrite it.",
            "Choose a smaller file or another path.",
        )
    return data


def decode(path: Path, data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid(
            path, f"{path} is not valid UTF-8; nothing was written.", f"Save {path} as UTF-8."
        ) from None


def _changed(path: Path) -> YojoError:
    return conflict(
        path,
        f"{path} changed while yojo was writing it; nothing was written.",
        "Run the same command again; it re-reads the file first.",
    )


def _new_file_mode() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


def _stage(path: Path, data: bytes, mode: int) -> Path:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    staged = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(staged, mode)
    except BaseException:
        with contextlib.suppress(OSError):
            staged.unlink()
        raise
    return staged


def publish(path: Path, data: bytes, expected: bytes | None) -> None:
    """Write ``data`` to ``path`` if it still holds ``expected`` (None: it must not exist)."""
    refuse_symlinks(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise _invalid(
            path, f"Cannot create the folder for {path}.", "Check the folder's permissions."
        ) from None
    refuse_symlinks(path)
    lock = path.with_name(f".{path.name}.yojo-lock")
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        raise conflict(
            path,
            f"Another yojo command is writing {path}; nothing was written.",
            f"Wait for it to finish and run the command again. If none is running, delete {lock}.",
            lock=str(lock),
        ) from None
    except OSError:
        raise _invalid(
            path, f"Cannot write in the folder of {path}.", "Check the folder's permissions."
        ) from None
    try:
        _publish_locked(path, data, expected)
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()


def _publish_locked(path: Path, data: bytes, expected: bytes | None) -> None:
    if expected is None:
        mode = _new_file_mode()
    else:
        try:
            info = path.lstat()
        except FileNotFoundError:
            raise _changed(path) from None
        except OSError:
            raise _invalid(
                path, f"Cannot inspect {path}.", "Check the path and its permissions."
            ) from None
        if not stat.S_ISREG(info.st_mode):
            refuse_symlinks(path)
            raise _changed(path)
        mode = stat.S_IMODE(info.st_mode)
    try:
        staged: Path | None = _stage(path, data, mode)
    except OSError:
        raise _invalid(
            path,
            f"Cannot write a temporary file next to {path}; nothing was written.",
            "Check the folder's permissions and free disk space.",
        ) from None
    assert staged is not None
    try:
        if read_current(path) != expected:
            raise _changed(path)
        if expected is None:
            try:
                os.link(staged, path)
            except FileExistsError:
                raise _changed(path) from None
            except OSError:
                raise _invalid(
                    path,
                    f"Cannot create {path} on this file system; nothing was written.",
                    "Choose a path on a local disk that supports hard links.",
                ) from None
        else:
            # Not a compare-and-swap: a non-yojo writer between the comparison above and this
            # rename loses its edit. Replacement is safe only while the file is quiet.
            try:
                os.replace(staged, path)
            except OSError:
                raise _invalid(
                    path,
                    f"Cannot replace {path}; it was left unchanged.",
                    "Check the file's permissions and that no other program holds it open.",
                ) from None
            staged = None
    finally:
        if staged is not None:
            with contextlib.suppress(OSError):
                staged.unlink()
