"""Managed-path safety: symlink rejection and directory-escape prevention.

Every path the pack/verify/install code reads or writes inside a managed root
is funneled through :func:`safe_join` / :func:`assert_no_links`. Symlinks are
refused at every path component (both inside the root and on the root itself),
and any resolved location that would leave the managed root is rejected before
the filesystem is touched. This applies to bundle contents, the wheel staging
directory, the input directory and the freshly created virtual environment.
"""

from __future__ import annotations

import os
from pathlib import Path


class PathSafetyError(ValueError):
    """Raised when a managed path contains a link or escapes its root."""


def assert_no_links(path: Path, *, root: Path | None = None) -> None:
    """Reject *path* if it or any relevant component is a symlink.

    The check uses ``os.lstat`` per component so a symlink is detected even
    when it is broken or points outside the machine. When *root* is given,
    only *path* and its ancestors down to *root* are inspected and each
    resolved location must stay inside *root* (no escape).
    """
    path = Path(path)
    import stat as _stat

    if root is None:
        chain = []
        cursor = Path(path.anchor) if path.anchor else Path()
        for part in path.parts:
            cursor = (
                cursor / part if str(cursor) or path.anchor else Path(part)
            )
            chain.append(cursor)
    else:
        stop = Path(root).resolve()
        chain = []
        cursor = Path(path)
        while True:
            chain.append(cursor)
            if cursor.resolve(strict=False) == stop or cursor == Path(root):
                break
            if cursor.parent == cursor:
                break
            cursor = cursor.parent

    for current in chain:
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            # A not-yet-created leaf is fine; existing parents were checked.
            continue
        if _stat.S_ISLNK(mode):
            raise PathSafetyError(
                f"symlink is not allowed in managed path: {current}"
            )
        if root is not None:
            stop = Path(root).resolve()
            resolved = current.resolve(strict=False)
            if resolved != stop and stop not in resolved.parents:
                raise PathSafetyError(
                    f"path {current} resolves outside managed root {stop}"
                )


def safe_join(root: Path, *parts: str) -> Path:
    """Join *parts* under *root*, refusing ``..`` escapes and symlinks."""
    root = Path(root)
    candidate = root
    for part in parts:
        if not part or part in (".",):
            continue
        if os.path.isabs(part):
            raise PathSafetyError(f"absolute segment is not allowed: {part}")
        candidate = candidate / part
        normalized = os.path.normpath(candidate)
        root_norm = os.path.normpath(root)
        if normalized != root_norm and not normalized.startswith(
            root_norm + os.sep
        ):
            raise PathSafetyError(f"path escapes managed root: {candidate}")
    assert_no_links(candidate, root=root)
    return candidate


def iter_regular_files(root: Path) -> list[Path]:
    """List regular files under *root* sorted, rejecting every symlink."""
    root = Path(root)
    assert_no_links(root)
    found: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dir_path = Path(directory)
        assert_no_links(dir_path, root=root)
        # os.walk with followlinks=False still lists symlinked directories;
        # drop them explicitly so they are never descended into.
        kept = []
        for name in dirnames:
            child = dir_path / name
            if child.is_symlink():
                raise PathSafetyError(
                    f"symlink is not allowed in managed path: {child}"
                )
            kept.append(name)
        dirnames[:] = sorted(kept)
        for name in sorted(filenames):
            child = dir_path / name
            if child.is_symlink():
                raise PathSafetyError(
                    f"symlink is not allowed in managed path: {child}"
                )
            if not child.is_file():
                raise PathSafetyError(f"not a regular file: {child}")
            found.append(child)
    return found


def relative_posix(root: Path, path: Path) -> str:
    """Return *path* relative to *root* as a forward-slash manifest key."""
    rel = Path(path).resolve().relative_to(Path(root).resolve())
    return rel.as_posix()
