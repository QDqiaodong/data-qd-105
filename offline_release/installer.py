"""``install``: offline installation into an isolated virtual environment.

The target interpreter need not provide pip or ensurepip: the installer first
creates an empty venv (``--without-pip``), then bootstraps pip *from the
bundled pip wheel* and installs the bundled wheels by explicit path. Every
subprocess runs isolated (``-I``) with proxy/index environment stripped and
``--no-index`` set, so no index access, source build or system site-package
can enter the environment.

Network enforcement does not rely on PYTHONPATH (ignored under ``-I``) or on
``sitecustomize`` (a distribution copy earlier on sys.path would win). A small
blocker module is loaded from an explicit file path by a private launcher,
which patches ``socket.socket``/``socket.create_connection`` before any
project or pip code runs.

Pre-flight verification happens before the environment is created. Any
installation or smoke-test failure removes this run's environment directory;
a pre-existing environment is refused up front, and the bundle is only read.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import read_manifest
from .paths import assert_no_links, safe_join
from .platform_target import target_from_interpreter
from .verifier import verify


class InstallError(ValueError):
    """Raised when pre-flight, bootstrap, installation or smoke test fails."""


_ENV_STRIP = {
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "PIP_CACHE_DIR",
    "PIP_CONFIG_FILE",
}

# Loaded by explicit file path, so it works under -I and cannot be shadowed
# by a distribution sitecustomize.py earlier on sys.path. Only connection
# establishment is patched: the raw socket constructor must stay intact
# because the stdlib ssl module builds sockets while it is being imported.
_BLOCKER_SOURCE = (
    "import socket as _socket\n"
    "_real_socket = _socket.socket\n"
    "_real_create_connection = _socket.create_connection\n"
    "def _blocked(*args, **kwargs):\n"
    "    raise OSError('network is disabled by the offline_release installer')\n"
    "class _BlockedSocket(_real_socket):\n"
    "    def connect(self, *args, **kwargs):\n"
    "        raise OSError('network is disabled by the offline_release installer')\n"
    "    def connect_ex(self, *args, **kwargs):\n"
    "        raise OSError('network is disabled by the offline_release installer')\n"
    "_socket.socket = _BlockedSocket\n"
    "_socket.create_connection = _blocked\n"
)

_LAUNCHER_SOURCE = """
import importlib.util
import runpy
import sys

mode = sys.argv[1]
blocker_spec = importlib.util.spec_from_file_location(
    "_offline_net_block", {blocker_path!r}
)
blocker = importlib.util.module_from_spec(blocker_spec)
blocker_spec.loader.exec_module(blocker)

if mode == "module":
    module = sys.argv[2]
    sys.argv = [module] + sys.argv[3:]
    runpy.run_module(module, run_name="__main__", alter_sys=True)
elif mode == "path":
    path = sys.argv[2]
    sys.argv = [path] + sys.argv[3:]
    runpy.run_path(path, run_name="__main__")
elif mode == "code":
    code = sys.argv[2]
    sys.argv = ["-c"] + sys.argv[3:]
    exec(compile(code, "<offline-release>", "exec"), {{"__name__": "__main__"}})
else:
    raise SystemExit(f"unknown launcher mode {{mode!r}}")
"""


@dataclass
class InstallResult:
    env_dir: Path
    python: Path
    target_triple: str
    installed: list = field(default_factory=list)
    smoke: dict = field(default_factory=dict)
    commands: list = field(default_factory=list)


def _offline_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _ENV_STRIP}
    # Hard pip policy: no index, no cache, wheels only. Explicit wheel paths
    # plus --no-index make any network or source-build path unreachable.
    env["PIP_NO_INDEX"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_CACHE_DIR"] = "1"
    return env


def _run(cmd: list, *, env: dict, label: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd) if cwd else None,
        check=False,
    )
    if proc.returncode != 0:
        raise InstallError(
            f"{label} failed (exit {proc.returncode})\n"
            f"command: {' '.join(cmd)}\n"
            f"stdout: {proc.stdout.strip()}\n"
            f"stderr: {proc.stderr.strip()}"
        )
    return proc.stdout


def _preflight(bundle: Path, python_executable: str, env_dir: Path):
    if not os.path.isabs(python_executable) and shutil.which(python_executable) is None:
        raise InstallError(f"interpreter not found: {python_executable}")
    target = target_from_interpreter(python_executable)
    result = verify(bundle, target=target)
    if not result.ok:
        raise InstallError(
            "bundle failed pre-flight verification:\n  - "
            + "\n  - ".join(result.problems)
        )
    if env_dir.exists():
        raise InstallError(
            f"environment directory already exists (refusing to overwrite): {env_dir}"
        )
    assert_no_links(env_dir.parent)
    return result, target


def _create_venv(python_executable: str, env_dir: Path, env: dict) -> Path:
    # --without-pip works even when the interpreter ships no ensurepip/pip.
    _run(
        [python_executable, "-I", "-m", "venv", "--without-pip", str(env_dir)],
        env=env,
        label="venv creation",
    )
    bin_dir = env_dir / ("Scripts" if os.name == "nt" else "bin")
    candidate = bin_dir / ("python.exe" if os.name == "nt" else "python")
    if not candidate.is_file():
        raise InstallError(f"venv interpreter missing after creation: {candidate}")
    return candidate


def _smoke_script() -> str:
    return """
import json, subprocess, sys, tempfile, shutil
from pathlib import Path

bundle = Path(sys.argv[1])
inputs = bundle / "inputs"
python = Path(sys.executable)
results = {}


def run_cli(module, args):
    proc = subprocess.run(
        [str(python), "-I", "-m", module, *args],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"smoke CLI {module} failed ({proc.returncode}): "
            f"{proc.stdout} {proc.stderr}"
        )
    return proc


work = Path(tempfile.mkdtemp(prefix="offline-smoke-"))
try:
    # 1. Acquisition: three-level nested rings -> 68 m2 parcel.
    acq_out = work / "acquisition"
    proc = run_cli("landcover_cube.acquisition", [
        "--network", str(inputs / "acquisition_network.json"),
        "--output", str(acq_out),
    ])
    acq_areas = json.loads(proc.stdout.strip())
    assert acq_areas["P_NEST"] == 68.0, acq_areas
    results["acquisition_nested_area_m2"] = acq_areas["P_NEST"]

    # 2. Baseline overlay on the original example -> 200 m2 total.
    cover_out = work / "coverage"
    run_cli("landcover_cube", [
        "--zones", str(inputs / "zones.geojson"),
        "--parcels", str(inputs / "parcels.geojson"),
        "--output", str(cover_out),
    ])
    report = json.loads((cover_out / "coverage.json").read_text())
    total = sum(zone["zone_area_m2"] for zone in report["zones"])
    assert abs(total - 200.0) < 1e-8, total
    results["overlay_total_zone_area_m2"] = total

    # 3. Tiled run key-by-key equivalent to the baseline on the same inputs.
    tiled_out = work / "tiled"
    run_cli("landcover_cube.tiled", [
        "--zones", str(inputs / "zones.geojson"),
        "--parcels", str(inputs / "parcels.geojson"),
        "--tile-size", "3",
        "--output", str(tiled_out),
    ])
    tiled_report = json.loads((tiled_out / "coverage.json").read_text())
    baseline = json.loads((cover_out / "coverage.json").read_text())

    def keyed(report):
        return {
            zone["zone_id"]: {
                key: zone[key]
                for key in (
                    "zone_area_m2", "classified_area_m2",
                    "unclassified_area_m2", "coverage_fraction", "class_area_m2",
                )
            }
            for zone in report["zones"]
        }

    assert keyed(tiled_report) == keyed(baseline), (tiled_report, baseline)
    results["tiled_equivalent"] = True

    # 4. Connectivity chain A-B-C: removing B loses 2 m2.
    conn_out = work / "connectivity.json"
    run_cli("landcover_cube.connectivity", [
        "--zones", str(inputs / "connectivity_zones.geojson"),
        "--parcels", str(inputs / "connectivity_parcels.geojson"),
        "--class", "forest",
        "--threshold", "1",
        "--output", str(conn_out),
    ])
    conn = json.loads(conn_out.read_text())
    ranking = conn["zones"][0]["vulnerability"]
    top = ranking[0]
    assert top["parcel_id"] == "B", ranking
    assert abs(top["absolute_loss_m2"] - 2.0) < 1e-8, ranking
    results["connectivity_top_removal_loss_m2"] = top["absolute_loss_m2"]

    import landcover_cube  # noqa: F401
    results["import_ok"] = True
finally:
    shutil.rmtree(work, ignore_errors=True)

print("SMOKE_OK " + json.dumps(results, sort_keys=True))
"""


def _run_smoke(venv_python: Path, launcher: Path, bundle: Path,
               env: dict, work_root: Path) -> dict:
    proc = subprocess.run(
        [
            str(venv_python), "-I", str(launcher), "code",
            _smoke_script(), str(bundle),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(work_root),
        check=False,
    )
    if proc.returncode != 0:
        raise InstallError(
            f"smoke test failed (exit {proc.returncode})\n"
            f"stdout: {proc.stdout.strip()}\nstderr: {proc.stderr.strip()}"
        )
    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith("SMOKE_OK ")),
        "",
    )
    if not line:
        raise InstallError(f"smoke test produced no result: {proc.stdout!r}")
    return json.loads(line.removeprefix("SMOKE_OK "))


def install(
    *,
    bundle: Path,
    python_executable: str,
    env_dir: Path,
    block_network: bool = True,
) -> InstallResult:
    """Verify *bundle*, create an isolated venv and install it fully offline."""
    bundle = Path(bundle).resolve()
    env_dir = Path(env_dir).resolve()
    assert_no_links(bundle)

    _, target = _preflight(bundle, python_executable, env_dir)
    manifest_data = read_manifest(bundle)

    wheel_paths = [
        safe_join(bundle, entry["file"])
        for entry in sorted(
            manifest_data["wheels"].values(), key=lambda item: item["file"]
        )
    ]
    pip_wheels = [p for p in wheel_paths if p.name.startswith("pip-")]
    if len(pip_wheels) != 1:
        raise InstallError("bundle must contain exactly one pip wheel")
    pip_wheel = pip_wheels[0]
    other_wheels = [p for p in wheel_paths if p != pip_wheel]

    env = _offline_env()
    created = False
    private_dir: Path | None = None
    commands: list = []
    try:
        venv_python = _create_venv(python_executable, env_dir, env)
        created = True
        commands.append(
            f"{python_executable} -I -m venv --without-pip {env_dir}"
        )

        # Private work/launcher directory next to the env; removed at the end.
        private_dir = Path(
            tempfile.mkdtemp(prefix="offline-install-", dir=str(env_dir.parent))
        )
        work_root = private_dir / "work"
        work_root.mkdir()
        launcher = private_dir / "launcher.py"
        blocker = private_dir / "net_block.py"
        if block_network:
            blocker.write_text(_BLOCKER_SOURCE, encoding="utf-8")
            launcher.write_text(
                _LAUNCHER_SOURCE.format(blocker_path=str(blocker)),
                encoding="utf-8",
            )

            def blocked(cmd, *, label: str):
                return _run(
                    [str(venv_python), "-I", str(launcher), *cmd],
                    env=env, label=label, cwd=work_root,
                )
        else:
            def blocked(cmd, *, label: str):
                return _run(
                    [str(venv_python), "-I", *cmd],
                    env=env, label=label, cwd=work_root,
                )

        # A wheel is a zip; <wheel>/pip runs the bundled package's __main__
        # with only the standard library (pip has no third-party runtime deps).
        blocked(
            ["path", f"{pip_wheel}{os.sep}pip", "install",
             "--no-index", "--no-cache-dir", "--no-deps",
             "--only-binary=:all:", str(pip_wheel)],
            label="pip bootstrap",
        )
        commands.append(
            f"bootstrap {pip_wheel.name} (--no-index, stdlib-only zip run)"
        )
        blocked(
            ["path", f"{pip_wheel}{os.sep}pip", "install",
             "--no-index", "--no-cache-dir",
             "--only-binary=:all:",  # source distributions cannot be built
             "--no-deps",
             *[str(wheel) for wheel in other_wheels]],
            label="wheel installation",
        )
        commands.append(
            "install --no-index --only-binary=:all: "
            + ", ".join(p.name for p in other_wheels)
        )
        if block_network:
            smoke = _run_smoke(venv_python, launcher, bundle, env, work_root)
        else:
            smoke = _run_smoke_plain(venv_python, bundle, env, work_root)
    except BaseException:
        # Failure: remove this run's half-built environment. The bundle is
        # read-only here and a pre-existing env was refused before creation.
        if created:
            shutil.rmtree(env_dir, ignore_errors=True)
        raise
    finally:
        if private_dir is not None:
            shutil.rmtree(private_dir, ignore_errors=True)

    return InstallResult(
        env_dir=env_dir,
        python=venv_python,
        target_triple=target.triple,
        installed=[p.name for p in wheel_paths],
        smoke=smoke,
        commands=commands,
    )


def _run_smoke_plain(venv_python: Path, bundle: Path,
                     env: dict, work_root: Path) -> dict:
    proc = subprocess.run(
        [str(venv_python), "-I", "-c", _smoke_script(), str(bundle)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(work_root),
        check=False,
    )
    if proc.returncode != 0:
        raise InstallError(
            f"smoke test failed (exit {proc.returncode})\n"
            f"stdout: {proc.stdout.strip()}\nstderr: {proc.stderr.strip()}"
        )
    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith("SMOKE_OK ")),
        "",
    )
    if not line:
        raise InstallError(f"smoke test produced no result: {proc.stdout!r}")
    return json.loads(line.removeprefix("SMOKE_OK "))
