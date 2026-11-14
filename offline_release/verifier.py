"""``verify``: independently re-check a release bundle.

Verification never trusts the manifest's claims: wheel bytes are reopened and
re-inspected, file lengths/SHA-256 are recomputed, the embedded pyproject and
lock file are re-parsed, and the dependency closure and target compatibility
are evaluated again. All managed paths are checked for symlinks and escapes
before reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import INPUTS_DIR, MANIFEST_NAME, META_DIR, TOOL_DIR, WHEELS_DIR
from .manifest import ManifestError, read_manifest, verify_file_hashes
from .paths import PathSafetyError, assert_no_links, iter_regular_files, safe_join
from .platform_target import Target, detect_target, wheel_compatible
from .wheels import (
    WheelError,
    inspect_wheel,
    parse_lock,
    parse_pyproject,
    validate_closure,
)


@dataclass
class VerifyResult:
    ok: bool
    target: Target
    problems: list = field(default_factory=list)
    wheel_count: int = 0
    input_count: int = 0
    project: str = ""
    version: str = ""

    def summary(self) -> str:
        head = (
            f"bundle {'VALID' if self.ok else 'INVALID'} for "
            f"{self.target.triple}: {self.wheel_count} wheels, "
            f"{self.input_count} inputs"
        )
        if self.project:
            head += f", {self.project}=={self.version}"
        if self.problems:
            return head + "\n  - " + "\n  - ".join(self.problems)
        return head


def _check_layout(bundle_root: Path) -> list:
    problems = []
    for rel in (WHEELS_DIR, INPUTS_DIR, META_DIR, TOOL_DIR):
        path = safe_join(bundle_root, rel)
        if not path.is_dir():
            problems.append(f"missing bundle directory: {rel}")
    manifest_path = safe_join(bundle_root, MANIFEST_NAME)
    if not manifest_path.is_file():
        problems.append(f"missing {MANIFEST_NAME}")
    return problems


def verify(bundle: Path, *, target: Target | None = None) -> VerifyResult:
    """Run every bundle check; problems are collected, never raised partially."""
    bundle = Path(bundle).resolve()
    problems = []
    try:
        assert_no_links(bundle)
    except PathSafetyError as exc:
        return VerifyResult(False, target or detect_target(), [str(exc)])
    if not bundle.is_dir():
        return VerifyResult(
            False, target or detect_target(), [f"bundle not found: {bundle}"]
        )

    problems.extend(_check_layout(bundle))
    try:
        manifest = read_manifest(bundle)
    except ManifestError as exc:
        return VerifyResult(
            False, target or detect_target(), problems + [str(exc)]
        )

    manifest_target = Target.from_dict(manifest["target"])
    effective_target = target or manifest_target
    if target is not None and target.triple != manifest_target.triple:
        problems.append(
            f"bundle target {manifest_target.triple} does not match requested "
            f"target {target.triple}"
        )

    # 1. Managed-path and on-disk hash/length verification.
    try:
        problems.extend(verify_file_hashes(bundle, manifest))
    except PathSafetyError as exc:
        problems.append(str(exc))

    # 2. Re-parse the embedded policy sources (not any repository checkout).
    try:
        project_meta = parse_pyproject(
            safe_join(bundle, META_DIR, "pyproject.toml")
        )
        runtime_lock = parse_lock(
            safe_join(bundle, META_DIR, "requirements.lock")
        )
    except (WheelError, PathSafetyError, OSError) as exc:
        return VerifyResult(
            False, effective_target, problems + [f"policy sources unreadable: {exc}"]
        )

    if project_meta["normalized"] != manifest["project"]["normalized"]:
        problems.append("manifest project name disagrees with embedded pyproject")
    if project_meta["version"] != manifest["project"]["version"]:
        problems.append("manifest project version disagrees with embedded pyproject")
    manifest_lock = {
        entry["name"]: entry["version"] for entry in manifest["runtime_lock"]
    }
    if manifest_lock != runtime_lock:
        problems.append("manifest runtime_lock disagrees with embedded requirements.lock")

    # 3. Every declared wheel must exist as a managed file and re-inspect clean.
    wheels = []
    wheel_dir = safe_join(bundle, WHEELS_DIR)
    on_disk = {path.name: path for path in iter_regular_files(wheel_dir)}
    declared = set(manifest["wheels"])
    if set(on_disk) != declared:
        missing = sorted(declared - set(on_disk))
        extra = sorted(set(on_disk) - declared)
        if missing:
            problems.append(f"wheels declared but absent: {missing}")
        if extra:
            problems.append(f"wheels present but undeclared: {extra}")
    for filename, entry in sorted(manifest["wheels"].items()):
        path = on_disk.get(filename)
        if path is None:
            continue
        try:
            record = inspect_wheel(path, effective_target)
        except WheelError as exc:
            problems.append(str(exc))
            continue
        record.file = entry["file"]
        record.role = entry["role"]
        wheels.append(record)
        # Cross-check manifest-recorded identity against freshly parsed bytes.
        for key in ("dist_name", "version", "python_tag", "abi_tag",
                    "platform_tag", "purelib"):
            if getattr(record, key) != entry[key]:
                problems.append(
                    f"{filename}: {key} changed vs manifest "
                    f"({getattr(record, key)!r} != {entry[key]!r})"
                )
        if record.sha256 != entry["sha256"] or record.size != entry["size"]:
            problems.append(f"{filename}: hash/length changed vs manifest")
        if manifest["files"].get(entry["file"], {}).get("sha256") != entry["sha256"]:
            problems.append(f"{filename}: not consistently recorded in file table")
        if not wheel_compatible(filename, effective_target):
            problems.append(
                f"{filename}: incompatible with target {effective_target.triple}"
            )

    # 4. Re-run the full dependency closure from first principles.
    if wheels:
        closure = validate_closure(
            wheels, project_meta, runtime_lock, effective_target
        )
        problems.extend(closure.problems)

    # 5. Inputs must be regular managed files matching their manifest records.
    try:
        actual_inputs = {p.name for p in iter_regular_files(safe_join(bundle, INPUTS_DIR))}
    except PathSafetyError as exc:
        actual_inputs = set()
        problems.append(str(exc))
    declared_inputs = set(manifest["inputs"])
    if actual_inputs != declared_inputs:
        problems.append(
            f"input set mismatch: missing {sorted(declared_inputs - actual_inputs)}, "
            f"extra {sorted(actual_inputs - declared_inputs)}"
        )
    for name, entry in manifest["inputs"].items():
        file_record = manifest["files"].get(entry["file"])
        if file_record is None or file_record["sha256"] != entry["sha256"]:
            problems.append(f"input {name}: manifest hash inconsistency")

    # 6. The embedded tool copy must itself be present and hash-covered.
    tool_files = [
        rel for rel in manifest["files"] if rel.startswith(TOOL_DIR + "/")
    ]
    if not tool_files:
        problems.append("embedded offline_release tool is missing")

    result = VerifyResult(
        ok=not problems,
        target=effective_target,
        problems=sorted(set(problems)),
        wheel_count=len(manifest["wheels"]),
        input_count=len(manifest["inputs"]),
        project=manifest["project"]["normalized"],
        version=manifest["project"]["version"],
    )
    return result
