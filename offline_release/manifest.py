"""Release bundle manifest: construction, serialization and verification.

The manifest is the single source of truth for a bundle. It binds the target
triple, the project identity, the lock/pyproject inputs, every wheel's
distribution identity/tags/dependencies and every managed file's length and
SHA-256. It is written last during ``pack`` and never trusts on-disk state
during ``verify``: all hashes are recomputed from the bytes present.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import MANIFEST_NAME, META_DIR
from .paths import assert_no_links, iter_regular_files, relative_posix
from .wheels import hash_file

MANIFEST_SCHEMA = 1


class ManifestError(ValueError):
    """Raised when a manifest is missing, malformed or no longer matches."""


def _wheel_entry(wheel, rel_file: str) -> dict:
    return {
        "file": rel_file,
        "filename": wheel.filename,
        "role": wheel.role,
        "dist_name": wheel.dist_name,
        "raw_name": wheel.raw_name,
        "version": wheel.version,
        "python_tag": wheel.python_tag,
        "abi_tag": wheel.abi_tag,
        "platform_tag": wheel.platform_tag,
        "tags": [list(tag) for tag in wheel.tags],
        "purelib": wheel.purelib,
        "requires_python": wheel.metadata_requires_python,
        "requires_dist": [
            {
                "name": req["name"],
                "normalized": req["normalized"],
                "extras": sorted(req["extras"]),
                "specifiers": [list(spec) for spec in req["specifiers"]],
                "marker": req["marker"],
            }
            for req in wheel.requires
        ],
        "size": wheel.size,
        "sha256": wheel.sha256,
    }


def build_manifest(
    *,
    bundle_root: Path,
    target,
    project_meta: dict,
    runtime_lock: dict,
    wheels,
    inputs_dir: Path,
) -> dict:
    """Hash every managed file under the bundle and assemble the manifest."""
    bundle_root = Path(bundle_root)
    files = {}
    for path in iter_regular_files(bundle_root):
        rel = relative_posix(bundle_root, path)
        if rel == MANIFEST_NAME:
            continue
        size, digest = hash_file(path)
        files[rel] = {"size": size, "sha256": digest}

    wheel_entries = {}
    for wheel in wheels:
        rel = relative_posix(bundle_root, bundle_root / wheel.file)
        entry = _wheel_entry(wheel, rel)
        # The per-file hash/length must agree with the generic file table.
        if files[rel]["sha256"] != wheel.sha256 or files[rel]["size"] != wheel.size:
            raise ManifestError(
                f"internal hash inconsistency for {wheel.filename}"
            )
        wheel_entries[wheel.filename] = entry

    input_entries = {}
    for path in iter_regular_files(Path(inputs_dir)):
        rel = relative_posix(bundle_root, path)
        input_entries[path.name] = {
            "file": rel,
            "size": files[rel]["size"],
            "sha256": files[rel]["sha256"],
        }

    manifest = {
        "manifest_schema": MANIFEST_SCHEMA,
        "target": target.to_dict(),
        "project": {
            "name": project_meta["name"],
            "normalized": project_meta["normalized"],
            "version": project_meta["version"],
            "requires_python": project_meta["requires_python"],
            "declared_dependencies": [
                {
                    "name": dep["name"],
                    "normalized": dep["normalized"],
                    "specifiers": [list(spec) for spec in dep["specifiers"]],
                    "marker": dep["marker"],
                }
                for dep in project_meta["dependencies"]
            ],
        },
        "runtime_lock": [
            {"name": name, "version": version}
            for name, version in sorted(runtime_lock.items())
        ],
        "meta_sources": {
            "pyproject": f"{META_DIR}/pyproject.toml",
            "requirements_lock": f"{META_DIR}/requirements.lock",
        },
        "wheels": wheel_entries,
        "inputs": input_entries,
        "files": files,
    }
    return manifest


def write_manifest(bundle_root: Path, manifest: dict) -> Path:
    path = Path(bundle_root) / MANIFEST_NAME
    text = json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False) + "\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def read_manifest(bundle_root: Path) -> dict:
    path = Path(bundle_root) / MANIFEST_NAME
    assert_no_links(path)
    if not path.is_file():
        raise ManifestError(f"missing {MANIFEST_NAME} in {bundle_root}")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{MANIFEST_NAME} is not valid JSON: {exc}") from exc
    if manifest.get("manifest_schema") != MANIFEST_SCHEMA:
        raise ManifestError(
            f"unsupported manifest schema {manifest.get('manifest_schema')!r}"
        )
    required = {"target", "project", "runtime_lock", "wheels", "files", "inputs"}
    missing = required - set(manifest)
    if missing:
        raise ManifestError(f"manifest missing keys: {sorted(missing)}")
    return manifest


def verify_file_hashes(bundle_root: Path, manifest: dict) -> list:
    """Recompute every recorded hash; return the list of mismatch problems."""
    bundle_root = Path(bundle_root)
    problems = []
    recorded = manifest["files"]
    actual_files = {}
    for path in iter_regular_files(bundle_root):
        rel = relative_posix(bundle_root, path)
        if rel == MANIFEST_NAME:
            continue
        actual_files[rel] = path
    for rel, record in sorted(recorded.items()):
        path = bundle_root / rel
        if not path.is_file() or path.is_symlink():
            problems.append(f"manifest file missing or replaced by link: {rel}")
            continue
        size, digest = hash_file(path)
        if size != record["size"]:
            problems.append(
                f"length mismatch for {rel}: {size} != {record['size']}"
            )
        if digest != record["sha256"]:
            problems.append(f"sha256 mismatch for {rel}")
    extras = sorted(set(actual_files) - set(recorded))
    for rel in extras:
        problems.append(f"file present but not declared in manifest: {rel}")
    return problems


def manifest_digest(manifest: dict) -> str:
    text = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
