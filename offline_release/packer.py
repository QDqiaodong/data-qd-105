"""``pack``: build an offline release bundle from prepared inputs.

The pack machine only reads from three explicitly provided locations:

* the prepared wheel staging directory (project wheel, locked runtime
  wheels, exactly one pip wheel),
* the synthetic input directory,
* the project's pyproject/requirements.lock copied in as verification sources.

Nothing is pulled from an installed environment or from elsewhere in the
repository tree. The output directory is created fresh and every write goes
through the managed-path helpers.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import INPUTS_DIR, META_DIR, TOOL_DIR, WHEELS_DIR
from .manifest import build_manifest, write_manifest
from .paths import assert_no_links, iter_regular_files, safe_join
from .platform_target import Target, detect_target
from .wheels import (
    WheelError,
    inspect_wheel,
    parse_lock,
    parse_pyproject,
    validate_closure,
)


class PackError(ValueError):
    """Raised when a bundle cannot be assembled from the given inputs."""


def _copy_file(src: Path, dst: Path) -> None:
    assert_no_links(src)
    if not src.is_file():
        raise PackError(f"not a regular file: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as inp, open(dst, "wb") as out:
        shutil.copyfileobj(inp, out, length=1024 * 1024)
    # Bundled artifacts never carry executable or write bits beyond 0644.
    os.chmod(dst, 0o644)


def _check_plain_directory(path: Path, label: str) -> None:
    path = Path(path)
    assert_no_links(path)
    if not path.is_dir():
        raise PackError(f"{label} directory does not exist: {path}")


def pack(
    *,
    wheel_dir: Path,
    input_dir: Path,
    pyproject: Path,
    requirements_lock: Path,
    output: Path,
    target: Target | None = None,
    tool_source: Path | None = None,
) -> dict:
    """Assemble a new release bundle at *output* and return its manifest."""
    wheel_dir = Path(wheel_dir).resolve()
    input_dir = Path(input_dir).resolve()
    pyproject = Path(pyproject).resolve()
    requirements_lock = Path(requirements_lock).resolve()
    output = Path(output).resolve()
    target = target or detect_target()
    tool_source = Path(tool_source) if tool_source else Path(__file__).parent

    _check_plain_directory(wheel_dir, "wheel staging")
    _check_plain_directory(input_dir, "input")
    assert_no_links(pyproject)
    assert_no_links(requirements_lock)
    if not pyproject.is_file() or not requirements_lock.is_file():
        raise PackError("pyproject.toml and requirements.lock must both be files")
    if output.exists():
        raise PackError(
            f"output bundle already exists (refusing to overwrite): {output}"
        )
    assert_no_links(output.parent)

    # Parse the policy sources before creating anything on disk.
    project_meta = parse_pyproject(pyproject)
    runtime_lock = parse_lock(requirements_lock)

    staged = iter_regular_files(wheel_dir)
    non_wheels = [p.name for p in staged if not p.name.endswith(".whl")]
    if non_wheels:
        raise PackError(
            f"wheel staging directory may contain wheels only; found: {non_wheels}"
        )
    if not staged:
        raise PackError("wheel staging directory is empty")

    wheels = []
    for path in staged:
        try:
            record = inspect_wheel(path, target)
        except WheelError as exc:
            raise PackError(str(exc)) from exc
        wheels.append(record)

    closure = validate_closure(wheels, project_meta, runtime_lock, target)
    if closure.problems:
        raise PackError("wheel policy failures:\n  - " + "\n  - ".join(closure.problems))

    # From here the bundle directory exists; any failure removes it entirely.
    created = False
    try:
        output.mkdir(parents=True)
        created = True
        wheels_root = safe_join(output, WHEELS_DIR)
        wheels_root.mkdir()
        inputs_root = safe_join(output, INPUTS_DIR)
        inputs_root.mkdir()
        meta_root = safe_join(output, META_DIR)
        meta_root.mkdir()

        for record, src in zip(wheels, staged):
            dst = safe_join(output, WHEELS_DIR, src.name)
            _copy_file(src, dst)
            record.file = dst.relative_to(output).as_posix()

        input_files = iter_regular_files(input_dir)
        if not input_files:
            raise PackError("input directory contains no files")
        seen_names = set()
        for src in input_files:
            if src.name in seen_names:
                raise PackError(
                    f"duplicate input file name after flattening: {src.name}"
                )
            seen_names.add(src.name)
            _copy_file(src, safe_join(output, INPUTS_DIR, src.name))

        _copy_file(pyproject, safe_join(output, META_DIR, "pyproject.toml"))
        _copy_file(
            requirements_lock,
            safe_join(output, META_DIR, "requirements.lock"),
        )

        # Embed the release tool itself so the target can verify/install with
        # only a bare interpreter (no pip, no repository checkout needed).
        tool_root = safe_join(output, TOOL_DIR)
        tool_root.mkdir()
        for src in iter_regular_files(tool_source):
            if src.suffix != ".py":
                continue
            _copy_file(src, safe_join(output, TOOL_DIR, src.name))

        manifest = build_manifest(
            bundle_root=output,
            target=target,
            project_meta=project_meta,
            runtime_lock=runtime_lock,
            wheels=wheels,
            inputs_dir=inputs_root,
        )
        write_manifest(output, manifest)
    except BaseException:
        if created:
            shutil.rmtree(output, ignore_errors=True)
        raise

    return manifest
