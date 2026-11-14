"""Command line interface: ``python -m offline_release {pack,verify,install}``.

The module imports only the standard library and the sibling modules of this
package, so the embedded copy inside a release bundle runs on a bare target
interpreter that has neither pip nor any third-party package installed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow ``python /path/to/bundle/offline_release/__main__.py ...`` on a bare
# target interpreter (where -m/cwd discovery may be unavailable) as well as
# the normal ``python -m offline_release ...`` invocation. The release bundle
# is read-only content: never drop bytecode caches next to its sources.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "offline_release"
sys.dont_write_bytecode = True

from . import __version__
from .installer import InstallError, install
from .manifest import manifest_digest, read_manifest
from .packer import PackError, pack
from .paths import PathSafetyError
from .platform_target import target_from_interpreter
from .verifier import verify


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="offline_release",
        description=(
            "Pack, verify and install the offline land-cover release bundle "
            "(CPython minor version / OS / architecture locked)."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"offline_release {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    pack_p = sub.add_parser(
        "pack",
        help="build a release bundle from a prepared wheel staging directory",
    )
    pack_p.add_argument("--wheel-dir", required=True, type=Path,
                        help="directory holding project/runtime/pip .whl files")
    pack_p.add_argument("--input-dir", required=True, type=Path,
                        help="directory of synthetic CLI input files to embed")
    pack_p.add_argument("--pyproject", required=True, type=Path,
                        help="project pyproject.toml (identity/deps source)")
    pack_p.add_argument("--requirements-lock", required=True, type=Path,
                        help="exact runtime lock (name==version lines)")
    pack_p.add_argument("--output", required=True, type=Path,
                        help="new bundle directory (must not exist)")

    verify_p = sub.add_parser(
        "verify", help="re-verify a bundle against its manifest and a target"
    )
    verify_p.add_argument("bundle", type=Path, help="release bundle directory")
    verify_p.add_argument(
        "--python",
        type=Path,
        default=None,
        help="interpreter whose target must match (default: bundle target)",
    )

    install_p = sub.add_parser(
        "install",
        help="create an isolated venv from the bundle without any network",
    )
    install_p.add_argument("bundle", type=Path, help="release bundle directory")
    install_p.add_argument("--python", required=True,
                           help="interpreter used to create the environment")
    install_p.add_argument("--env-dir", required=True, type=Path,
                           help="new environment directory (must not exist)")
    install_p.add_argument(
        "--allow-network",
        action="store_true",
        help="disable the in-process socket blocker (--no-index stays in force)",
    )
    return parser


def _cmd_pack(args) -> int:
    manifest = pack(
        wheel_dir=args.wheel_dir,
        input_dir=args.input_dir,
        pyproject=args.pyproject,
        requirements_lock=args.requirements_lock,
        output=args.output,
    )
    print(
        json.dumps(
            {
                "packed": str(args.output),
                "target": manifest["target"]["triple"],
                "project": "{}=={}".format(
                    manifest["project"]["normalized"],
                    manifest["project"]["version"],
                ),
                "wheels": sorted(manifest["wheels"]),
                "inputs": sorted(manifest["inputs"]),
                "manifest_sha256": manifest_digest(manifest),
            },
            sort_keys=True,
            indent=2,
        )
    )
    return 0


def _cmd_verify(args) -> int:
    # A missing/unreadable path is a usage error (2); an existing bundle that
    # fails its checks is an invalid-bundle result (1).
    if not args.bundle.exists() or args.bundle.is_symlink():
        print(f"verify failed: bundle not found: {args.bundle}", file=sys.stderr)
        return 2
    target = None
    if args.python is not None:
        target = target_from_interpreter(str(args.python))
    result = verify(args.bundle, target=target)
    print(result.summary())
    if result.ok:
        manifest = read_manifest(args.bundle)
        print(f"manifest_sha256: {manifest_digest(manifest)}")
    return 0 if result.ok else 1


def _cmd_install(args) -> int:
    result = install(
        bundle=args.bundle,
        python_executable=str(args.python),
        env_dir=args.env_dir,
        block_network=not args.allow_network,
    )
    print(
        json.dumps(
            {
                "installed_at": str(result.env_dir),
                "python": str(result.python),
                "target": result.target_triple,
                "wheels": result.installed,
                "smoke": result.smoke,
                "steps": result.commands,
            },
            sort_keys=True,
            indent=2,
        )
    )
    return 0


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "pack": _cmd_pack,
        "verify": _cmd_verify,
        "install": _cmd_install,
    }
    try:
        return handlers[args.command](args)
    except (PackError, InstallError, PathSafetyError) as exc:
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"{args.command} failed: missing path: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
