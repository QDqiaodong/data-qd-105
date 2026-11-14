"""Wheel inspection and bundle dependency closure validation.

Everything here uses only the standard library. A wheel is treated as
untrusted input: the zip central directory is checked, every entry is
constrained to the wheel directory (no absolute paths, no ``..``, no symlink
flag), ``RECORD`` hashes/sizes are recomputed, and distribution identity is
cross-checked between the filename and ``METADATA``/``WHEEL``.

The bundle policy implemented here rejects:

* anything that is not a ``.whl`` (sdist tarballs, eggs, loose files),
* missing wheels required by the project or the runtime lock,
* more than one wheel version per normalized distribution,
* corrupted archives or RECORD mismatches,
* wheels whose tags do not match the bundle target.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import zipfile
from dataclasses import dataclass, field
from email.parser import Parser
from pathlib import Path

from .markers import (
    marker_applies,
    marker_environment,
    normalize_name,
    parse_requirement,
    specifier_matches,
)
from .platform_target import Target, wheel_compatible, wheel_filename_tags


class WheelError(ValueError):
    """Raised for any malformed, corrupt or policy-breaking wheel."""


@dataclass
class WheelRecord:
    """Validated facts about one wheel inside a bundle."""

    file: str                      # manifest-relative posix path
    filename: str                  # PEP 427 basename
    dist_name: str                 # normalized
    raw_name: str
    version: str
    python_tag: str
    abi_tag: str
    platform_tag: str
    tags: list
    purelib: bool
    requires: list                # parsed PEP 508 dicts
    size: int
    sha256: str
    is_pip: bool = False
    is_project: bool = False
    extra: bool = False
    role: str = "runtime"         # project | runtime | pip | extra
    metadata_requires_python: str = ""


# --- Low level wheel checks ------------------------------------------------


def _dist_info_dir(names: list[str], filename: str) -> str:
    dist_dirs = {
        name.split("/")[0]
        for name in names
        if "/" in name and name.split("/")[1] == "" and name.split("/")[0].endswith(
            ".dist-info"
        )
    }
    dist_dirs |= {
        parts[0]
        for parts in (name.split("/") for name in names)
        if len(parts) >= 2 and parts[0].endswith(".dist-info")
    }
    if len(dist_dirs) != 1:
        raise WheelError(
            f"{filename}: expected exactly one .dist-info directory, found "
            f"{sorted(dist_dirs)!r}"
        )
    return dist_dirs.pop()


def _read_text(zf: zipfile.ZipFile, name: str) -> str:
    try:
        raw = zf.read(name)
    except KeyError as exc:
        raise WheelError(f"missing required member {name}") from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WheelError(f"{name} is not valid UTF-8") from exc


def _check_member_paths(zf: zipfile.ZipFile, filename: str) -> None:
    for info in zf.infolist():
        name = info.filename
        if name.startswith("/") or name.startswith("\\") or ".." in name.split("/"):
            raise WheelError(f"{filename}: unsafe member path {name!r}")
        # Unix symlink entries carry the link flag in the external attributes.
        mode = (info.external_attr >> 16) & 0xFFFF
        import stat as _stat

        if _stat.S_ISLNK(mode):
            raise WheelError(f"{filename}: symlink member is forbidden: {name}")
        if info.flag_bits & 0x800:
            # UTF-8 flag is fine; nothing to do, but keep this explicit.
            pass
        if os.path.isabs(name):
            raise WheelError(f"{filename}: absolute member path {name!r}")


def _check_zip_containment(path: Path, filename: str) -> None:
    """Reject bytes outside the declared zip structure.

    A wheel's file length must be fully explained by its local headers,
    central directory and end-of-central-directory record. Appended junk
    (a common tamper/concatenation trick) is invisible to CRC checks because
    it belongs to no member, so the structural boundaries are checked
    explicitly from the end-of-central-directory records.
    """
    import struct

    data = path.read_bytes()
    size = len(data)
    eocd = data.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise WheelError(f"{filename}: missing end-of-central-directory record")
    if len(data) - eocd < 22:
        raise WheelError(f"{filename}: truncated end-of-central-directory record")
    (disk_no, cd_disk, entries_disk, total_entries,
     cd_size, cd_offset, comment_len) = struct.unpack(
        "<HHHHIIH", data[eocd + 4:eocd + 22]
    )
    if eocd + 22 + comment_len != size:
        raise WheelError(f"{filename}: trailing bytes after end-of-central-directory")
    actual_cd_start = eocd
    if cd_offset == 0xFFFFFFFF or total_entries == 0xFFFF or cd_size == 0xFFFFFFFF:
        # ZIP64: locate the zip64 EOCD locator and record.
        locator = data.rfind(b"PK\x06\x07")
        if locator < 0 or locator + 20 > eocd:
            raise WheelError(f"{filename}: broken zip64 structure")
        z64_record_offset = struct.unpack("<Q", data[locator + 8:locator + 16])[0]
        if data[z64_record_offset:z64_record_offset + 4] != b"PK\x06\x06":
            raise WheelError(f"{filename}: broken zip64 end record")
        cd_size, cd_offset = struct.unpack(
            "<QQ", data[z64_record_offset + 40:z64_record_offset + 56]
        )
        actual_cd_start = z64_record_offset
    if cd_offset + cd_size != actual_cd_start:
        raise WheelError(
            f"{filename}: central directory does not end where it begins "
            f"(gap or overlap in the archive)"
        )
    # The first member must start at byte zero: no prefixed junk either.
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        if infos and infos[0].header_offset != 0:
            raise WheelError(f"{filename}: leading bytes before first zip member")


def _verify_record(zf: zipfile.ZipFile, record_name: str, filename: str) -> None:
    text = _read_text(zf, record_name)
    reader = csv.reader(io.StringIO(text))
    seen = set()
    for row in reader:
        if not row:
            continue
        if len(row) < 3:
            raise WheelError(f"{filename}: malformed RECORD row {row!r}")
        member, hash_field, size_field = row[0], row[1], row[2]
        if member in seen:
            raise WheelError(f"{filename}: duplicate RECORD entry {member}")
        seen.add(member)
        if member == record_name:
            # RECORD itself is not hashed.
            continue
        try:
            info = zf.getinfo(member)
        except KeyError as exc:
            raise WheelError(
                f"{filename}: RECORD lists missing member {member}"
            ) from exc
        if size_field:
            if int(size_field) != info.file_size:
                raise WheelError(
                    f"{filename}: size mismatch for {member}: "
                    f"{info.file_size} != {size_field}"
                )
        if hash_field:
            algo, _, digest = hash_field.partition("=")
            if algo != "sha256":
                raise WheelError(
                    f"{filename}: unsupported RECORD hash algorithm {algo}"
                )
            actual = hashlib.sha256(zf.read(member)).digest()
            actual_b64 = base64.urlsafe_b64encode(actual).rstrip(b"=").decode()
            if actual_b64 != digest:
                raise WheelError(
                    f"{filename}: RECORD sha256 mismatch for {member}"
                )
    # Every non-directory member must appear in RECORD.
    missing = [
        info.filename
        for info in zf.infolist()
        if not info.is_dir() and info.filename not in seen
    ]
    if missing:
        raise WheelError(
            f"{filename}: members absent from RECORD: {sorted(missing)!r}"
        )


def inspect_wheel(path: Path, target: Target) -> WheelRecord:
    """Validate one wheel file and return its recorded facts."""
    filename = path.name
    if not filename.endswith(".whl"):
        raise WheelError(f"{filename}: not a wheel (only .whl files are accepted)")
    try:
        stem_parts = filename[:-4].split("-")
        if len(stem_parts) < 5:
            raise ValueError("too few components")
        raw_name, version = stem_parts[0], stem_parts[1]
        py_tag, abi_tag, platform_tag = stem_parts[-3:]
    except ValueError as exc:
        raise WheelError(f"{filename}: malformed PEP 427 wheel filename") from exc

    if not wheel_compatible(filename, target):
        raise WheelError(
            f"{filename}: no tag {wheel_filename_tags(filename)} is compatible "
            f"with target {target.triple}"
        )

    if not path.is_file() or path.is_symlink():
        raise WheelError(f"{filename}: not a regular file")

    _check_zip_containment(path, filename)

    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)

    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise WheelError(f"{filename}: corrupt or unreadable zip archive") from exc
    with zf:
        bad = zf.testzip()
        if bad is not None:
            raise WheelError(f"{filename}: CRC failure in member {bad}")
        names = zf.namelist()
        _check_member_paths(zf, filename)
        dist_dir = _dist_info_dir(names, filename)
        metadata_text = _read_text(zf, f"{dist_dir}/METADATA")
        wheel_text = _read_text(zf, f"{dist_dir}/WHEEL")
        record_name = f"{dist_dir}/RECORD"
        if record_name not in names:
            raise WheelError(f"{filename}: missing {record_name}")
        _verify_record(zf, record_name, filename)

    metadata = Parser().parsestr(metadata_text)
    meta_name = metadata.get("Name", "").strip()
    meta_version = metadata.get("Version", "").strip()
    if not meta_name or not meta_version:
        raise WheelError(f"{filename}: METADATA lacks Name/Version")
    if normalize_name(meta_name) != normalize_name(raw_name):
        raise WheelError(
            f"{filename}: filename name {raw_name!r} disagrees with METADATA "
            f"Name {meta_name!r}"
        )
    if meta_version != version:
        raise WheelError(
            f"{filename}: filename version {version!r} disagrees with METADATA "
            f"Version {meta_version!r}"
        )

    wheel_meta = Parser().parsestr(wheel_text)
    if wheel_meta.get("Wheel-Version", "").strip() not in {"1.0", "1.1"}:
        raise WheelError(f"{filename}: unsupported Wheel-Version")
    purelib = wheel_meta.get("Root-Is-Purelib", "false").strip().lower() == "true"
    wheel_tags = [
        tuple(line.split(":", 1)[1].strip().split("-"))
        for line in wheel_text.splitlines()
        if line.startswith("Tag:")
    ]
    wheel_tags = [t for t in wheel_tags if len(t) == 3]
    if not wheel_tags:
        raise WheelError(f"{filename}: WHEEL metadata declares no tags")
    # The filename compresses dot-separated tag alternatives; WHEEL lists
    # each expanded triple. Require every filename combination to be present.
    filename_tag_set = {
        (py, abi, plat)
        for py in py_tag.split(".")
        for abi in abi_tag.split(".")
        for plat in platform_tag.split(".")
    }
    if not filename_tag_set.issubset(set(wheel_tags)):
        raise WheelError(
            f"{filename}: filename tags {sorted(filename_tag_set)} are not "
            f"present in WHEEL metadata {wheel_tags!r}"
        )

    requires = []
    for line in metadata_text.splitlines():
        if line.startswith("Requires-Dist:"):
            requires.append(parse_requirement(line.split(":", 1)[1]))
    requires_python = metadata.get("Requires-Python", "").strip()

    return WheelRecord(
        file="",
        filename=filename,
        dist_name=normalize_name(meta_name),
        raw_name=meta_name,
        version=meta_version,
        python_tag=py_tag,
        abi_tag=abi_tag,
        platform_tag=platform_tag,
        tags=wheel_tags,
        purelib=purelib,
        requires=requires,
        size=size,
        sha256=digest.hexdigest(),
        metadata_requires_python=requires_python,
    )


# --- Lock / pyproject parsing ----------------------------------------------


def parse_lock(path: Path) -> dict:
    """Parse a flat ``name==version`` lock file into ``{normalized: version}``."""
    locked = {}
    for lineno, raw in enumerate(Path(path).read_text().splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        req = parse_requirement(line)
        ops = [op for op, _ in req["specifiers"]]
        if ops != ["=="] or len(req["specifiers"]) != 1 or req["marker"]:
            raise WheelError(
                f"{path}:{lineno}: runtime lock entries must be exact 'name==version'"
            )
        name = req["normalized"]
        version = req["specifiers"][0][1]
        if name in locked and locked[name] != version:
            raise WheelError(f"{path}:{lineno}: duplicate lock entry for {name}")
        locked[name] = version
    return locked


def parse_pyproject(path: Path) -> dict:
    """Read project identity, runtime deps and requires-python from pyproject."""
    import tomllib

    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    project = data.get("project")
    if not project:
        raise WheelError(f"{path}: missing [project] table")
    name = project.get("name")
    version = project.get("version")
    if not name or not version:
        raise WheelError(f"{path}: project.name and project.version are required")
    requires_python = project.get("requires-python", "")
    deps = [parse_requirement(dep) for dep in project.get("dependencies", [])]
    return {
        "name": name,
        "normalized": normalize_name(name),
        "version": str(version),
        "requires_python": requires_python,
        "dependencies": deps,
    }


# --- Closure / policy validation -------------------------------------------


@dataclass
class ClosureReport:
    wheels: list = field(default_factory=list)
    project: WheelRecord | None = None
    pip: WheelRecord | None = None
    by_name: dict = field(default_factory=dict)
    problems: list = field(default_factory=list)


def _check_requires_python(spec_text: str, target: Target) -> bool:
    from .markers import parse_specifier

    if not spec_text:
        return True
    try:
        specs = parse_specifier(spec_text)
    except ValueError:
        return False
    return specifier_matches(specs, f"{target.cpython_minor}.0")


def validate_closure(
    wheels: list,
    project_meta: dict,
    runtime_lock: dict,
    target: Target,
) -> ClosureReport:
    """Apply the full bundle wheel policy and return a structured report."""
    report = ClosureReport(wheels=wheels)
    by_name: dict = {}
    for wheel in wheels:
        wheel.is_pip = wheel.dist_name == "pip"
        wheel.is_project = wheel.dist_name == project_meta["normalized"]
        if wheel.dist_name in by_name:
            report.problems.append(
                f"multiple wheel versions for {wheel.dist_name}: "
                f"{by_name[wheel.dist_name].filename} and {wheel.filename}"
            )
            continue
        by_name[wheel.dist_name] = wheel
    report.by_name = by_name

    pips = [w for w in wheels if w.is_pip]
    if len(pips) != 1:
        report.problems.append(
            f"expected exactly one pip wheel, found {len(pips)}"
        )
    else:
        report.pip = pips[0]
        pips[0].role = "pip"

    projects = [w for w in wheels if w.is_project]
    if len(projects) != 1:
        report.problems.append(
            f"expected exactly one project wheel for "
            f"{project_meta['normalized']}, found {len(projects)}"
        )
    else:
        report.project = projects[0]
        projects[0].role = "project"
        if projects[0].version != project_meta["version"]:
            report.problems.append(
                f"project wheel version {projects[0].version} disagrees with "
                f"pyproject version {project_meta['version']}"
            )
        if not _check_requires_python(
            project_meta["requires_python"], target
        ) or not _check_requires_python(
            projects[0].metadata_requires_python, target
        ):
            report.problems.append(
                f"project requires-python {project_meta['requires_python']!r} "
                f"is not satisfied by {target.cpython_minor}"
            )

    env = marker_environment(target)

    # Every lock entry must exist, at the exact locked version.
    for name, version in sorted(runtime_lock.items()):
        wheel = by_name.get(name)
        if wheel is None:
            report.problems.append(
                f"missing wheel for locked dependency {name}=={version}"
            )
            continue
        if wheel.version != version:
            report.problems.append(
                f"wheel version for {name} is {wheel.version}, lock requires "
                f"{version}"
            )
        wheel.role = "runtime"

    # pyproject declared dependencies must agree with the lock.
    locked_view = {dep["normalized"]: dep for dep in project_meta["dependencies"]}
    for dep in project_meta["dependencies"]:
        if not marker_applies(dep["marker"], env):
            continue
        wheel = by_name.get(dep["normalized"])
        if wheel is None:
            report.problems.append(
                f"project dependency {dep['name']} has no wheel in the bundle"
            )
            continue
        if not specifier_matches(dep["specifiers"], wheel.version):
            report.problems.append(
                f"wheel {wheel.filename} does not satisfy project requirement "
                f"{dep['name']} {dep['specifiers']}"
            )
        if dep["normalized"] not in runtime_lock:
            report.problems.append(
                f"project dependency {dep['name']} is absent from requirements.lock"
            )
        elif not specifier_matches([("==", runtime_lock[dep["normalized"]])],
                                   wheel.version):
            report.problems.append(
                f"wheel for {dep['normalized']} disagrees with the locked version"
            )

    # Reverse direction: lock entries that are not declared deps are still
    # legitimate transitive pins, but an unknown extra wheel is rejected.
    known = {project_meta["normalized"], "pip"} | set(runtime_lock)
    for wheel in wheels:
        if wheel.dist_name not in known:
            wheel.extra = True
            wheel.role = "extra"
            report.problems.append(
                f"wheel {wheel.filename} is neither the project, pip nor a "
                f"locked runtime dependency"
            )

    # Full requirement closure over markers, with versions satisfied.
    for wheel in wheels:
        if wheel.is_pip:
            continue
        for req in wheel.requires:
            if not marker_applies(req["marker"], env):
                continue
            dep_wheel = by_name.get(req["normalized"])
            if dep_wheel is None:
                report.problems.append(
                    f"{wheel.filename} requires {req['name']} but no matching "
                    f"wheel is bundled"
                )
                continue
            if not specifier_matches(req["specifiers"], dep_wheel.version):
                report.problems.append(
                    f"{wheel.filename} requires {req['name']}{req['specifiers']} "
                    f"but bundle provides {dep_wheel.version}"
                )

    return report


def hash_file(path: Path) -> tuple[int, str]:
    """Return (size, sha256-hex) for a regular file."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()
