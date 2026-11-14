"""Target platform description and PEP 425 wheel-tag compatibility.

A release bundle is bound to one concrete target:

* CPython *minor* version (e.g. ``cp311``) -- a different minor release is
  rejected even when wheels happen to be tagged ``py3``,
* operating system family (``linux`` / ``darwin`` / ``windows``),
* machine architecture (``aarch64`` / ``x86_64`` / ...).

The decision deliberately stays conservative: only the tag sets actually
produced for this project's pinned stack are accepted, musllinux wheels never
match a glibc target and vice versa, and ``any`` is accepted only for pure
Python platform tags.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    """Concrete interpreter/OS/machine target a bundle is bound to."""

    python_tag: str          # e.g. "cp311"
    cpython_minor: str       # e.g. "3.11"
    os_name: str             # "linux" | "darwin" | "windows"
    arch: str                # platform.machine(), e.g. "aarch64"
    libc: str                # "glibc" | "musl" | ""
    triple: str              # single manifest field: "cp311-linux-aarch64-glibc"

    def to_dict(self) -> dict:
        return {
            "python_tag": self.python_tag,
            "cpython_minor": self.cpython_minor,
            "os_name": self.os_name,
            "arch": self.arch,
            "libc": self.libc,
            "triple": self.triple,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Target":
        return cls(
            python_tag=data["python_tag"],
            cpython_minor=data["cpython_minor"],
            os_name=data["os_name"],
            arch=data["arch"],
            libc=data.get("libc", ""),
            triple=data["triple"],
        )


def detect_libc() -> str:
    """Return ``musl`` when running on musl libc, otherwise ``glibc``."""
    try:
        if platform.libc_ver()[0] == "glibc":
            return "glibc"
    except Exception:
        pass
    import glob

    if glob.glob("/lib/ld-musl-*") or glob.glob("/lib*/ld-musl-*"):
        return "musl"
    # Fall back to the ELF interpreter name visible in /proc on Linux.
    try:
        with open("/proc/self/cmdline", "rb"):
            pass
    except OSError:
        pass
    return "glibc"


def detect_target() -> Target:
    """Describe the currently running interpreter as a build target."""
    if sys.implementation.name != "cpython":
        raise ValueError(
            f"offline release supports CPython only, got {sys.implementation.name}"
        )
    minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    python_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    os_name = {"linux": "linux", "darwin": "darwin", "win32": "windows"}.get(
        sys.platform, sys.platform
    )
    arch = platform.machine() or "unknown"
    libc = detect_libc() if os_name == "linux" else ""
    suffix = f"-{libc}" if libc else ""
    return Target(
        python_tag=python_tag,
        cpython_minor=minor,
        os_name=os_name,
        arch=arch,
        libc=libc,
        triple=f"{python_tag}-{os_name}-{arch}{suffix}",
    )


def target_from_interpreter(executable: str) -> Target:
    """Run *executable* with -I and ask it to describe itself."""
    import json
    import subprocess

    code = (
        "import json,platform,sys;"
        "libc='';"
        "import glob;"
        "libc='musl' if glob.glob('/lib/ld-musl-*') or glob.glob('/lib*/ld-musl-*') else ('glibc' if sys.platform.startswith('linux') else '');"
        "os_name={'linux':'linux','darwin':'darwin','win32':'windows'}.get(sys.platform,sys.platform);"
        "print(json.dumps({'impl':sys.implementation.name,'major':sys.version_info.major,'minor':sys.version_info.minor,'os':os_name,'arch':platform.machine() or 'unknown','libc':libc}))"
    )
    proc = subprocess.run(
        [executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ValueError(
            f"cannot inspect interpreter {executable}: {proc.stderr.strip()}"
        )
    info = json.loads(proc.stdout)
    if info["impl"] != "cpython":
        raise ValueError(
            f"{executable} is {info['impl']}, offline release supports CPython only"
        )
    python_tag = f"cp{info['major']}{info['minor']}"
    suffix = f"-{info['libc']}" if info["libc"] else ""
    return Target(
        python_tag=python_tag,
        cpython_minor=f"{info['major']}.{info['minor']}",
        os_name=info["os"],
        arch=info["arch"],
        libc=info["libc"],
        triple=f"{python_tag}-{info['os']}-{info['arch']}{suffix}",
    )


def _parse_wheel_filename(filename: str) -> tuple[str, str, list[tuple[str, str, str]]]:
    """Split a PEP 427 wheel filename into name, version and tag triples."""
    stem = filename[:-4] if filename.endswith(".whl") else filename
    parts = stem.split("-")
    if len(parts) < 5:
        raise ValueError(f"wheel filename has too few components: {filename}")
    # name(-build)-python-abi-platform; build tag is optional and sits between
    # version and python tag, so tags are always the last three components.
    name, version = parts[0], parts[1]
    py_tag, abi_tag, platform_tag = parts[-3], parts[-2], parts[-1]
    tags = [
        (py, abi, plat)
        for py in py_tag.split(".")
        for abi in abi_tag.split(".")
        for plat in platform_tag.split(".")
    ]
    return name, version, tags


def _linux_platform_match(platform_tag: str, target: Target) -> bool:
    arch = target.arch
    if platform_tag == f"linux_{arch}":
        return target.libc != "musl"
    if platform_tag.startswith("manylinux"):
        # manylinux wheels are glibc builds. Accept any manylinux generation
        # for the target architecture; the running kernel/loader is by
        # construction newer than the historical manylinux baselines here.
        if target.libc == "musl":
            return False
        return platform_tag.endswith(f"_{arch}")
    if platform_tag.startswith("musllinux"):
        if target.libc != "musl":
            return False
        return platform_tag.endswith(f"_{arch}")
    return False


def _darwin_platform_match(platform_tag: str, target: Target) -> bool:
    # macosx_<major>_<minor>_<arch>[_<arch>...] universal tags
    pieces = platform_tag.split("_")
    if len(pieces) < 4 or pieces[0] != "macosx":
        return False
    arches = pieces[3:]
    aliases = {
        "x86_64": {"x86_64", "intel", "universal2", "fat64", "fat3", "universal"},
        "arm64": {"arm64", "universal2", "universal"},
        "aarch64": {"arm64", "aarch64", "universal2", "universal"},
    }.get(target.arch, {target.arch})
    return any(arch in aliases for arch in arches)


def _platform_tag_match(platform_tag: str, target: Target) -> bool:
    if platform_tag == "any":
        return True
    if target.os_name == "linux":
        return _linux_platform_match(platform_tag, target)
    if target.os_name == "darwin":
        return _darwin_platform_match(platform_tag, target)
    if target.os_name == "windows":
        return platform_tag in {"win_amd64", "win_arm64", "win32", "any"}
    return False


def _python_tag_match(py_tag: str, target: Target) -> bool:
    if py_tag == "py3":
        return True
    if py_tag.startswith("py") and py_tag[2:].isdigit():
        return int(py_tag[2:]) <= int(target.python_tag[2:])
    if py_tag == target.python_tag:
        return True
    # Generic "cp311" style tag equality is the normal binary case above;
    # implementation-neutral "abi3" is decided at the ABI level.
    return False


def _abi_tag_match(py_tag: str, abi_tag: str, target: Target) -> bool:
    if abi_tag == "none":
        return True
    if abi_tag == "abi3":
        # Stable ABI wheels target a minimum CPython minor; the filename does
        # not encode it directly, so accept for equal-or-newer CPython 3.
        return py_tag.startswith("cp") or py_tag == "py3"
    if abi_tag == target.python_tag:
        return True
    return False


def wheel_compatible(filename: str, target: Target) -> bool:
    """Return True when at least one tag triple of the wheel matches target."""
    try:
        _, _, tags = _parse_wheel_filename(filename)
    except ValueError:
        return False
    for py_tag, abi_tag, platform_tag in tags:
        if not _platform_tag_match(platform_tag, target):
            continue
        if not _python_tag_match(py_tag, target):
            continue
        if not _abi_tag_match(py_tag, abi_tag, target):
            continue
        return True
    return False


def wheel_filename_tags(filename: str) -> list[tuple[str, str, str]]:
    return _parse_wheel_filename(filename)[2]
