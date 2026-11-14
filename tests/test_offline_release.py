"""Tests for the pure-stdlib offline_release pack/verify/install tool.

The lightweight cases build synthetic wheels by hand and need no third-party
package, so they run on the bare system interpreter (which has no pip). The
full end-to-end installation case is enabled only when a prepared wheel
staging directory containing a real pip wheel is supplied through the
OFFLINE_RELEASE_TEST_STAGING environment variable; it is skipped otherwise.
"""

import base64
import csv
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from offline_release.installer import InstallError, install
from offline_release.manifest import read_manifest, verify_file_hashes
from offline_release.markers import (
    marker_applies,
    marker_environment,
    normalize_name,
    parse_requirement,
    specifier_matches,
)
from offline_release.packer import PackError, pack
from offline_release.paths import PathSafetyError, safe_join
from offline_release.platform_target import (
    Target,
    detect_target,
    wheel_compatible,
)
from offline_release.verifier import verify
from offline_release.wheels import WheelError, inspect_wheel, parse_lock

TARGET = detect_target()
REAL_STAGING = os.environ.get("OFFLINE_RELEASE_TEST_STAGING")
REAL_PROJECT = os.environ.get("OFFLINE_RELEASE_TEST_PROJECT")


# --- Synthetic wheel factory ----------------------------------------------


def _record_hash(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def make_wheel(
    directory: Path,
    name: str,
    version: str,
    *,
    py_tag: str = "py3",
    abi_tag: str = "none",
    platform_tag: str = "any",
    requires_dist=(),
    requires_python: str = "",
    purelib: bool = True,
    extra_files: dict | None = None,
    corrupt_zip: bool = False,
    tamper_record: bool = False,
    omit_record: bool = False,
) -> Path:
    """Write a minimal but standards-shaped wheel into *directory*."""
    filename = f"{name.replace('-', '_')}-{version}-{py_tag}-{abi_tag}-{platform_tag}.whl"
    path = Path(directory) / filename
    dist_info = f"{name.replace('-', '_')}-{version}.dist-info"
    package = name.replace("-", "_")
    members: dict[str, bytes] = {
        f"{package}/__init__.py": f'__version__ = "{version}"\n'.encode(),
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\n"
            f"Name: {name}\n"
            f"Version: {version}\n"
            + (f"Requires-Python: {requires_python}\n" if requires_python else "")
            + "".join(f"Requires-Dist: {req}\n" for req in requires_dist)
        ).encode(),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            f"Root-Is-Purelib: {'true' if purelib else 'false'}\n"
            f"Tag: {py_tag}-{abi_tag}-{platform_tag}\n"
        ).encode(),
    }
    if extra_files:
        for rel, data in extra_files.items():
            members[rel] = data.encode() if isinstance(data, str) else data
    record_rows = [
        (member, _record_hash(data), str(len(data)))
        for member, data in members.items()
    ]
    if tamper_record:
        first = record_rows[0]
        record_rows[0] = (first[0], "sha256=" + "A" * 43, first[2])
    if not omit_record:
        buf = io.StringIO()
        writer = csv.writer(buf)
        for row in record_rows:
            writer.writerow(row)
        writer.writerow((f"{dist_info}/RECORD", "", ""))
        members[f"{dist_info}/RECORD"] = buf.getvalue().encode()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for member, data in members.items():
            zf.writestr(member, data)
    raw = buffer.getvalue()
    if corrupt_zip:
        raw = raw[:-50] + b"\x00" * 50
    path.write_bytes(raw)
    return path


def write_project_sources(root: Path, name: str = "demo-proj", version: str = "1.2.3",
                          deps=("shapely==2.1.2",)):
    pyproject = (
        '[build-system]\nrequires=["setuptools==80.9.0"]\n'
        'build-backend="setuptools.build_meta"\n\n[project]\n'
        f'name="{name}"\nversion="{version}"\nrequires-python=">=3.11"\n'
        f"dependencies={json.dumps(list(deps))}\n"
    )
    (root / "pyproject.toml").write_text(pyproject)
    (root / "requirements.lock").write_text(
        "".join(f"{line}\n" for line in deps)
    )


class WheelFactoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_synthetic_wheel_passes_inspection(self):
        wheel = make_wheel(
            self.tmp, "demo-proj", "1.2.3",
            requires_dist=("shapely==2.1.2",),
        )
        record = inspect_wheel(wheel, TARGET)
        self.assertEqual(record.dist_name, "demo-proj")
        self.assertEqual(record.version, "1.2.3")
        self.assertTrue(record.purelib)
        self.assertEqual(record.requires[0]["normalized"], "shapely")

    def test_corrupt_zip_rejected(self):
        wheel = make_wheel(self.tmp, "bad", "1.0", corrupt_zip=True)
        with self.assertRaises(WheelError):
            inspect_wheel(wheel, TARGET)

    def test_trailing_junk_bytes_rejected(self):
        wheel = make_wheel(self.tmp, "bad", "1.0")
        wheel.write_bytes(wheel.read_bytes() + b"appended-after-eocd")
        with self.assertRaises(WheelError):
            inspect_wheel(wheel, TARGET)

    def test_leading_junk_bytes_rejected(self):
        wheel = make_wheel(self.tmp, "bad", "1.0")
        wheel.write_bytes(b"PREFIX-JUNK" + wheel.read_bytes())
        with self.assertRaises(WheelError):
            inspect_wheel(wheel, TARGET)

    def test_record_tamper_rejected(self):
        wheel = make_wheel(self.tmp, "bad", "1.0", tamper_record=True)
        with self.assertRaises(WheelError):
            inspect_wheel(wheel, TARGET)

    def test_missing_record_rejected(self):
        wheel = make_wheel(self.tmp, "bad", "1.0", omit_record=True)
        with self.assertRaises(WheelError):
            inspect_wheel(wheel, TARGET)

    def test_non_wheel_rejected(self):
        (self.tmp / "thing-1.0.tar.gz").write_bytes(b"not a wheel")
        with self.assertRaises(WheelError):
            inspect_wheel(self.tmp / "thing-1.0.tar.gz", TARGET)


class PlatformTagTests(unittest.TestCase):
    def test_native_and_pure_wheels_match(self):
        arch = TARGET.arch
        self.assertTrue(
            wheel_compatible(
                f"numpy-2.2.6-cp311-cp311-manylinux2014_{arch}.whl", TARGET
            )
        )
        self.assertTrue(wheel_compatible("pip-24.3.1-py3-none-any.whl", TARGET))

    def test_foreign_architecture_rejected(self):
        other = "x86_64" if TARGET.arch != "x86_64" else "aarch64"
        self.assertFalse(
            wheel_compatible(
                f"numpy-2.2.6-cp311-cp311-manylinux2014_{other}.whl", TARGET
            )
        )

    def test_foreign_cpython_minor_rejected(self):
        other_minor = "cp310" if TARGET.python_tag != "cp310" else "cp312"
        self.assertFalse(
            wheel_compatible(
                f"p-1-{other_minor}-{other_minor}-linux_{TARGET.arch}.whl", TARGET
            )
        )

    def test_musl_and_glibc_wheels_never_mix(self):
        if TARGET.os_name != "linux":
            self.skipTest("libc distinction is Linux-specific")
        musl = "p-1-cp311-cp311-musllinux_1_1_" + TARGET.arch + ".whl"
        glibc = "p-1-cp311-cp311-manylinux_2_17_" + TARGET.arch + ".whl"
        if TARGET.libc == "glibc":
            self.assertFalse(wheel_compatible(musl, TARGET))
            self.assertTrue(wheel_compatible(glibc, TARGET))
        else:
            self.assertTrue(wheel_compatible(musl, TARGET))
            self.assertFalse(wheel_compatible(glibc, TARGET))

    def test_target_dataclass_roundtrip(self):
        data = TARGET.to_dict()
        again = Target.from_dict(data)
        self.assertEqual(again, TARGET)


class MarkerAndSpecifierTests(unittest.TestCase):
    def test_specifiers(self):
        self.assertTrue(specifier_matches(parse_lock_text("a==1.2"), "1.2.0"))
        self.assertFalse(specifier_matches(parse_lock_text("a==1.2"), "1.3.0"))
        self.assertTrue(
            specifier_matches([(">=", "1.0"), ("<", "2.0")], "1.5")
        )
        self.assertFalse(
            specifier_matches([(">=", "1.0"), ("<", "2.0")], "2.0")
        )
        self.assertTrue(specifier_matches([("~=", "1.4.1")], "1.4.9"))
        self.assertFalse(specifier_matches([("~=", "1.4.1")], "1.5.0"))

    def test_marker_matrix(self):
        env = marker_environment(TARGET)
        self.assertTrue(
            marker_applies('python_version == "%s"' % TARGET.cpython_minor, env)
        )
        self.assertFalse(marker_applies('sys_platform == "plan9"', env))
        self.assertTrue(
            marker_applies(
                '(os_name == "posix" or os_name == "nt") and '
                'not python_version < "3.10"',
                env,
            )
        )
        self.assertTrue(
            marker_applies('platform_machine in "%s x86_64"' % TARGET.arch, env)
        )
        self.assertFalse(marker_applies('"zzz" in platform_machine', env))

    def test_requirement_parsing(self):
        req = parse_requirement('Shapely.Extra[speedup]>=2.0; python_version>="3.9"')
        self.assertEqual(req["normalized"], "shapely-extra")
        self.assertEqual(req["extras"], {"speedup"})
        self.assertEqual(req["specifiers"], [(">=", "2.0")])
        self.assertIn("python_version", req["marker"])
        self.assertEqual(normalize_name("My_Pkg.ext"), "my-pkg-ext")


def parse_lock_text(text):
    line = text
    req = parse_requirement(line)
    return req["specifiers"]


class NetworkBlockerTests(unittest.TestCase):
    """The connect-time blocker must cut network without breaking ssl import."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        from offline_release.installer import _BLOCKER_SOURCE, _LAUNCHER_SOURCE
        self.blocker = self.tmp / "net_block.py"
        self.blocker.write_text(_BLOCKER_SOURCE)
        self.launcher = self.tmp / "launcher.py"
        self.launcher.write_text(
            _LAUNCHER_SOURCE.format(blocker_path=str(self.blocker))
        )

    def _run(self, code: str):
        import subprocess
        return subprocess.run(
            [sys.executable, "-I", str(self.launcher), "code", code],
            capture_output=True, text=True, check=False,
        )

    def test_ssl_imports_but_connections_are_blocked(self):
        code = (
            "import ssl, socket\n"
            "assert ssl.OPENSSL_VERSION\n"  # ssl needs the raw ctor at import
            "try:\n"
            "    socket.create_connection(('127.0.0.1', 9), timeout=2)\n"
            "    raise SystemExit('create_connection was not blocked')\n"
            "except OSError:\n"
            "    pass\n"
            "sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "try:\n"
            "    sock.connect(('127.0.0.1', 9))\n"
            "    raise SystemExit('socket.connect was not blocked')\n"
            "except OSError:\n"
            "    pass\n"
            "try:\n"
            "    sock.connect_ex(('127.0.0.1', 9))\n"
            "    raise SystemExit('connect_ex was not blocked')\n"
            "except OSError:\n"
            "    pass\n"
            "print('BLOCKER_OK')\n"
        )
        proc = self._run(code)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("BLOCKER_OK", proc.stdout)

    def test_blocker_loads_by_explicit_path_under_isolated_mode(self):
        # -I sets sys.flags.isolated; the explicit-path loader must still
        # patch sockets without any PYTHONPATH/site-package discovery.
        proc = self._run(
            "import sys, socket; assert sys.flags.isolated == 1; "
            "socket.create_connection.__name__; print('ISOLATED_OK')"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ISOLATED_OK", proc.stdout)


class PathSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_safe_join_blocks_escape_and_absolute(self):
        with self.assertRaises(PathSafetyError):
            safe_join(self.tmp, "..", "escape")
        with self.assertRaises(PathSafetyError):
            safe_join(self.tmp, "/etc/passwd")

    def test_symlink_rejected(self):
        target_dir = self.tmp / "real"
        target_dir.mkdir()
        (target_dir / "file").write_text("x")
        link = self.tmp / "link"
        link.symlink_to(target_dir)
        with self.assertRaises(PathSafetyError):
            safe_join(self.tmp, "link", "file")

    def test_symlinked_file_in_tree_rejected(self):
        root = self.tmp / "root"
        root.mkdir()
        outside = self.tmp / "outside.txt"
        outside.write_text("secret")
        (root / "inside.txt").symlink_to(outside)
        with self.assertRaises(PathSafetyError):
            safe_join(root, "inside.txt")


class PackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.wheel_dir = self.tmp / "wheels"
        self.wheel_dir.mkdir()
        self.input_dir = self.tmp / "inputs"
        self.input_dir.mkdir()
        (self.input_dir / "sample.json").write_text('{"ok": true}\n')
        write_project_sources(self.tmp)
        self.bundle = self.tmp / "bundle"
        arch = TARGET.arch
        # Runtime dependency as a binary wheel for this exact target.
        make_wheel(
            self.wheel_dir, "shapely", "2.1.2",
            py_tag=TARGET.python_tag, abi_tag=TARGET.python_tag,
            platform_tag=f"manylinux2014_{arch}" if TARGET.os_name == "linux" else "any",
            purelib=False,
        )
        make_wheel(self.wheel_dir, "pip", "24.3.1")
        make_wheel(self.wheel_dir, "demo-proj", "1.2.3",
                   requires_dist=("shapely==2.1.2",))

    def _pack(self):
        return pack(
            wheel_dir=self.wheel_dir,
            input_dir=self.input_dir,
            pyproject=self.tmp / "pyproject.toml",
            requirements_lock=self.tmp / "requirements.lock",
            output=self.bundle,
        )

    def test_pack_happy_path_and_manifest(self):
        manifest = self._pack()
        self.assertTrue(self.bundle.is_dir())
        self.assertEqual(manifest["target"]["triple"], TARGET.triple)
        self.assertEqual(
            set(manifest["wheels"]),
            {
                "demo_proj-1.2.3-py3-none-any.whl",
                "shapely-2.1.2-" + TARGET.python_tag + "-" + TARGET.python_tag
                + "-manylinux2014_" + TARGET.arch + ".whl",
                "pip-24.3.1-py3-none-any.whl",
            },
        )
        for entry in manifest["wheels"].values():
            self.assertIn("sha256", entry)
            self.assertIsInstance(entry["size"], int)
            self.assertTrue(entry["requires_dist"] is not None)
        # The tool itself is embedded and hash-covered.
        tool_files = [f for f in manifest["files"] if f.startswith("offline_release/")]
        self.assertTrue(tool_files)
        # Re-verification succeeds with no network and only stdlib.
        result = verify(self.bundle)
        self.assertTrue(result.ok, result.problems)

    def test_pack_refuses_existing_output(self):
        self.bundle.mkdir()
        with self.assertRaises(PackError):
            self._pack()

    def test_pack_rejects_source_distribution(self):
        (self.wheel_dir / "shapely-2.1.2.tar.gz").write_bytes(b"sdist")
        with self.assertRaises(PackError):
            self._pack()

    def test_missing_locked_dependency_rejected(self):
        next(self.wheel_dir.glob("shapely-*.whl")).unlink()
        with self.assertRaises(PackError) as ctx:
            self._pack()
        self.assertIn("missing wheel for locked dependency", str(ctx.exception))

    def test_duplicate_versions_rejected(self):
        make_wheel(
            self.wheel_dir, "shapely", "9.9.9",
            py_tag=TARGET.python_tag, abi_tag=TARGET.python_tag,
            platform_tag=f"manylinux2014_{TARGET.arch}", purelib=False,
        )
        with self.assertRaises(PackError) as ctx:
            self._pack()
        self.assertIn("multiple wheel versions", str(ctx.exception))

    def test_extra_unlocked_wheel_rejected(self):
        make_wheel(self.wheel_dir, "rogue", "0.0.1")
        with self.assertRaises(PackError) as ctx:
            self._pack()
        self.assertIn("neither the project, pip nor a locked", str(ctx.exception))

    def test_lock_version_mismatch_rejected(self):
        (self.tmp / "requirements.lock").write_text("shapely==9.9.9\n")
        with self.assertRaises(PackError):
            self._pack()

    def test_foreign_platform_wheel_rejected_at_pack(self):
        other_arch = "x86_64" if TARGET.arch != "x86_64" else "aarch64"
        next(self.wheel_dir.glob("shapely-*.whl")).unlink()
        make_wheel(
            self.wheel_dir, "shapely", "2.1.2",
            py_tag=TARGET.python_tag, abi_tag=TARGET.python_tag,
            platform_tag=f"manylinux2014_{other_arch}", purelib=False,
        )
        with self.assertRaises(PackError):
            self._pack()

    def test_corrupt_wheel_rejected_at_pack(self):
        next(self.wheel_dir.glob("shapely-*.whl")).unlink()
        make_wheel(
            self.wheel_dir, "shapely", "2.1.2",
            py_tag=TARGET.python_tag, abi_tag=TARGET.python_tag,
            platform_tag=f"manylinux2014_{TARGET.arch}", purelib=False,
            corrupt_zip=True,
        )
        with self.assertRaises(PackError):
            self._pack()

    def test_symlink_in_wheel_dir_rejected(self):
        outside = self.tmp / "outside.whl"
        make_wheel(self.tmp, "outside", "1.0")
        (self.wheel_dir / "outside-1.0-py3-none-any.whl").symlink_to(outside)
        with self.assertRaises((PackError, PathSafetyError)):
            self._pack()

    def test_failed_pack_leaves_no_bundle(self):
        next(self.wheel_dir.glob("shapely-*.whl")).unlink()
        with self.assertRaises(PackError):
            self._pack()
        self.assertFalse(self.bundle.exists())


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.wheel_dir = self.tmp / "wheels"
        self.wheel_dir.mkdir()
        self.input_dir = self.tmp / "inputs"
        self.input_dir.mkdir()
        (self.input_dir / "sample.json").write_text('{"ok": true}\n')
        write_project_sources(self.tmp)
        make_wheel(self.wheel_dir, "pip", "24.3.1")
        make_wheel(
            self.wheel_dir, "shapely", "2.1.2",
            py_tag=TARGET.python_tag, abi_tag=TARGET.python_tag,
            platform_tag=f"manylinux2014_{TARGET.arch}", purelib=False,
        )
        make_wheel(self.wheel_dir, "demo-proj", "1.2.3",
                   requires_dist=("shapely==2.1.2",))
        self.bundle = self.tmp / "bundle"
        pack(
            wheel_dir=self.wheel_dir, input_dir=self.input_dir,
            pyproject=self.tmp / "pyproject.toml",
            requirements_lock=self.tmp / "requirements.lock",
            output=self.bundle,
        )

    def test_verify_valid_bundle(self):
        result = verify(self.bundle)
        self.assertTrue(result.ok, result.problems)

    def test_verify_detects_tampered_file(self):
        wheel = next((self.bundle / "wheels").glob("pip-*.whl"))
        data = wheel.read_bytes()
        wheel.write_bytes(data + b" ")
        result = verify(self.bundle)
        self.assertFalse(result.ok)
        self.assertTrue(any("sha256 mismatch" in p for p in result.problems))

    def test_verify_detects_undeclared_file(self):
        (self.bundle / "wheels" / "intruder-1.0-py3-none-any.whl").write_bytes(b"x")
        result = verify(self.bundle)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("not declared in manifest" in p for p in result.problems)
        )

    def test_verify_detects_removed_file(self):
        next((self.bundle / "wheels").glob("pip-*.whl")).unlink()
        result = verify(self.bundle)
        self.assertFalse(result.ok)

    def test_verify_target_mismatch(self):
        foreign = Target(
            python_tag="cp310", cpython_minor="3.10",
            os_name=TARGET.os_name, arch=TARGET.arch, libc=TARGET.libc,
            triple="cp310-" + TARGET.os_name + "-" + TARGET.arch,
        )
        result = verify(self.bundle, target=foreign)
        self.assertFalse(result.ok)
        self.assertTrue(any("does not match requested target" in p
                            for p in result.problems))

    def test_manifest_file_table_consistency(self):
        manifest = read_manifest(self.bundle)
        problems = verify_file_hashes(self.bundle, manifest)
        self.assertEqual(problems, [])


@unittest.skipUnless(
    REAL_STAGING and REAL_PROJECT and Path(REAL_STAGING).is_dir(),
    "set OFFLINE_RELEASE_TEST_STAGING and OFFLINE_RELEASE_TEST_PROJECT for e2e",
)
class EndToEndInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.staging = self.tmp / "staging"
        shutil.copytree(REAL_STAGING, self.staging)
        self.inputs = self.tmp / "inputs"
        self.inputs.mkdir()
        for path in Path(REAL_PROJECT, "data").iterdir():
            if path.is_file():
                shutil.copy2(path, self.inputs / path.name)
        self.bundle = self.tmp / "bundle"
        pack(
            wheel_dir=self.staging,
            input_dir=self.inputs,
            pyproject=Path(REAL_PROJECT) / "pyproject.toml",
            requirements_lock=Path(REAL_PROJECT) / "requirements.lock",
            output=self.bundle,
        )
        self.env_dir = self.tmp / "env"

    def test_full_install_with_pip_less_interpreter_and_smoke(self):
        result = install(
            bundle=self.bundle,
            python_executable=sys.executable,
            env_dir=self.env_dir,
        )
        self.assertEqual(result.smoke["acquisition_nested_area_m2"], 68.0)
        self.assertEqual(result.smoke["overlay_total_zone_area_m2"], 200.0)
        self.assertTrue(result.smoke["tiled_equivalent"])
        self.assertEqual(
            result.smoke["connectivity_top_removal_loss_m2"], 2.0
        )
        # The new environment is isolated and importable.
        proc = self._run(
            "import sys, landcover_cube, numpy, shapely, pip;"
            "assert sys.base_prefix != sys.prefix;"
            "print(landcover_cube.__name__, numpy.__version__, shapely.__version__)"
        )
        self.assertIn("landcover_cube", proc)

    def _run(self, code):
        import subprocess
        proc = subprocess.run(
            [str(self.env_dir / "bin" / "python"), "-I", "-c", code],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_pre_existing_env_is_refused_and_preserved(self):
        self.env_dir.mkdir()
        marker = self.env_dir / "keep-me"
        marker.write_text("old environment")
        with self.assertRaises(InstallError):
            install(
                bundle=self.bundle,
                python_executable=sys.executable,
                env_dir=self.env_dir,
            )
        self.assertTrue(marker.exists())

    def test_failed_smoke_removes_this_env_and_keeps_bundle(self):
        from offline_release import installer

        def boom(*args, **kwargs):
            raise InstallError("simulated smoke failure")

        original = installer._run_smoke
        installer._run_smoke = boom
        try:
            with self.assertRaises(InstallError):
                install(
                    bundle=self.bundle,
                    python_executable=sys.executable,
                    env_dir=self.env_dir,
                )
        finally:
            installer._run_smoke = original
        self.assertFalse(self.env_dir.exists())
        self.assertTrue(self.bundle.is_dir())

    def test_preflight_failure_creates_no_environment(self):
        wheel = next((self.bundle / "wheels").glob("pip-*.whl"))
        wheel.write_bytes(wheel.read_bytes() + b"tampered")
        with self.assertRaises(InstallError):
            install(
                bundle=self.bundle,
                python_executable=sys.executable,
                env_dir=self.env_dir,
            )
        self.assertFalse(self.env_dir.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
