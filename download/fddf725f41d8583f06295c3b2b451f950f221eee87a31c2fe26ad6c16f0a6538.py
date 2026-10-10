"""Prepare a Windows x64 / CPython 3.14 wheel bundle on a networked machine.

Example (macOS preparation host):

    python scripts/prepare_windows_offline.py --bundle .cache/windows-offline --with-datahub-lite \
        --pip-python .venv-datahub/bin/python

The target computer needs no network, Docker, or source database. DataHub Lite
is optional and must be installed in a separate environment from route 2.
Only Python wheels, pinned requirements, and their hashes enter the bundle.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

try:
    from packaging.markers import default_environment
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
except ImportError:
    # pip vendors packaging; use it when the preparation Python lacks the
    # standalone package. No target-side dependency is added by this script.
    from pip._vendor.packaging.markers import default_environment
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.utils import canonicalize_name


PROJECT = Path(__file__).resolve().parents[1]
TARGET = ["--platform", "win_amd64", "--python-version", "3.14", "--implementation", "cp", "--abi", "cp314", "--only-binary", ":all:"]
DATAHUB_REQUIREMENT = "acryl-datahub[datahub-lite]==1.7.0.12"


def _run(command, *, capture=False, input_text=None):
    result = subprocess.run(command, text=True, input=input_text, stdout=subprocess.PIPE if capture else None, check=True)
    return result.stdout if capture else None


def _sha256(path):
    hash_obj = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hash_obj.update(block)
    return hash_obj.hexdigest()


def _target_environment():
    environment = default_environment()
    environment.update(
        os_name="nt",
        sys_platform="win32",
        platform_system="Windows",
        platform_machine="AMD64",
        platform_python_implementation="CPython",
        implementation_name="cpython",
        implementation_version="3.14.0",
        python_version="3.14",
        python_full_version="3.14.0",
    )
    return environment


def _logical_lines(text):
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1].strip() + " "
            continue
        yield pending + line
        pending = ""
    if pending:
        raise ValueError("Unterminated requirements continuation")


def _target_requirements(exported, *, require_hashes):
    selected = {}
    for line in _logical_lines(exported):
        head, *hashes = re.split(r"\s+--hash=", line)
        requirement = Requirement(head.strip())
        if requirement.marker and not requirement.marker.evaluate(environment=_target_environment()):
            continue
        if requirement.url or len(requirement.specifier) != 1 or next(iter(requirement.specifier)).operator != "==":
            raise ValueError(f"uv.lock did not export an exact pin: {head}")
        normalized_name = canonicalize_name(requirement.name)
        if normalized_name in selected:
            raise ValueError(f"Duplicate target package: {requirement.name}")
        requirement_text = f"{normalized_name}=={next(iter(requirement.specifier)).version}"
        if require_hashes:
            valid_hashes = sorted(set(value.strip() for value in hashes))
            if not valid_hashes or any(not re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in valid_hashes):
                raise ValueError(f"Missing/invalid lock hash for {requirement.name}")
            requirement_text += " " + " ".join("--hash=" + value for value in valid_hashes)
        selected[normalized_name] = requirement_text
    if not selected:
        raise ValueError("No Windows CPython 3.14 requirements in uv.lock")
    return [selected[key] for key in sorted(selected)]


def _pip(python, *args):
    return [str(python), "-m", "pip", *args]


def _download(python, wheelhouse, requirements, cache, *, offline_only=False, spec=None):
    command = _pip(python, "download", "--quiet", "--dest", str(wheelhouse), *TARGET, "--find-links", str(cache))
    if spec:
        command.append(spec)
    else:
        command.extend(["-r", str(requirements)])
    # First try the existing local wheel cache without network. A failed
    # attempt only means a wheel is missing; the online retry fills that gap.
    try:
        _run([*command, "--no-index"])
    except subprocess.CalledProcessError:
        if offline_only:
            raise
        _run(command)


def _verify_offline(python, wheelhouse, requirements=None, spec=None):
    with tempfile.TemporaryDirectory(prefix="ontology-win-wheel-verify-") as temp:
        command = _pip(python, "download", "--quiet", "--no-index", "--find-links", str(wheelhouse), "--dest", temp, *TARGET)
        if requirements:
            command.extend(["-r", str(requirements)])
        if spec:
            command.append(spec)
        _run(command)


def _build_route2_wheel(wheelhouse, version, *, offline_only=False):
    command = ["uv", "build", "--wheel", "--no-build-logs", "--out-dir", str(wheelhouse), str(PROJECT)]
    if offline_only:
        command.append("--offline")
    _run(command)
    wheels = sorted(wheelhouse.glob(f"ontology_route2-{version}-*.whl"))
    if len(wheels) != 1:
        raise ValueError(f"Expected exactly one ontology-route2 wheel for {version}")
    wheel = wheels[0]
    if not wheel.name.endswith("-py3-none-any.whl"):
        raise ValueError(f"Route 2 wheel must be pure Python: {wheel.name}")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    metadata_prefix = f"ontology_route2-{version}.dist-info/"
    if not names or any(not (name.startswith("ontology_r2/") or name.startswith(metadata_prefix)) for name in names):
        raise ValueError("Route 2 wheel contains files outside its package/metadata")
    if "ontology_r2/viewer.html" not in names:
        raise ValueError("Route 2 wheel is missing the local viewer")
    return wheel


def prepare(bundle, cache, pip_python, *, with_datahub_lite=False, offline_only=False):
    bundle, cache = Path(bundle).resolve(), Path(cache).resolve()
    wheelhouse = bundle / "wheelhouse"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    # Keep the venv's executable path: resolving its symlink can switch pip
    # to the underlying base interpreter and lose the venv's site-packages.
    pip_python = Path(pip_python).absolute()
    if not pip_python.is_file():
        raise FileNotFoundError(f"Preparation Python not found: {pip_python}")
    _run(_pip(pip_python, "--version"))
    project_meta = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
    route2_version = project_meta["project"]["version"]

    export = ["uv", "export", "--project", str(PROJECT), "--locked", "--no-dev", "--no-emit-project", "--format", "requirements.txt", "--no-header", "--no-annotate", "--quiet"]
    if offline_only:
        export.append("--offline")
    simple = _target_requirements(_run([*export, "--no-hashes"], capture=True), require_hashes=False)
    hashed = _target_requirements(_run(export, capture=True), require_hashes=True)
    if [line.split(" ", 1)[0] for line in hashed] != simple:
        raise ValueError("Hashed and unhashed lock exports differ")
    locked_file = bundle / "requirements-route2-locked.txt"
    locked_file.write_text("\n".join(simple) + "\n", encoding="utf-8")
    hashed_file = bundle / "requirements-route2-hashed.txt"
    hashed_file.write_text("\n".join(hashed) + "\n", encoding="utf-8")

    route2_wheel = _build_route2_wheel(wheelhouse, route2_version, offline_only=offline_only)
    _download(pip_python, wheelhouse, locked_file, cache, offline_only=offline_only)
    _verify_offline(pip_python, wheelhouse, hashed_file)
    _verify_offline(pip_python, wheelhouse, locked_file, f"ontology-route2=={route2_version}")

    datahub_file = None
    if with_datahub_lite:
        datahub_file = bundle / "requirements-datahub-lite-locked.txt"
        datahub_input = bundle / "requirements-datahub-lite.in"
        datahub_input.write_text(DATAHUB_REQUIREMENT + "\n", encoding="utf-8")
        compile_command = ["uv", "pip", "compile", str(datahub_input), "--python-platform", "x86_64-pc-windows-msvc", "--python-version", "3.14", "--only-binary", ":all:", "--no-header", "--no-annotate", "--output-file", str(datahub_file), "--quiet", "--find-links", str(cache)]
        try:
            _run([*compile_command, "--no-index"])
        except subprocess.CalledProcessError:
            if offline_only:
                raise
            _run(compile_command)
        _download(pip_python, wheelhouse, datahub_file, cache, offline_only=offline_only)
        # Verify DataHub's closure independently; it is installed in another
        # venv and may legitimately require different transitive versions.
        _verify_offline(pip_python, wheelhouse, datahub_file)
        _verify_offline(pip_python, wheelhouse, spec=DATAHUB_REQUIREMENT)

    files = [locked_file, hashed_file, route2_wheel, *sorted(wheelhouse.glob("*.whl"))]
    if datahub_file:
        files.extend([datahub_file, bundle / "requirements-datahub-lite.in"])
    unique_files = sorted(set(files), key=lambda path: str(path.relative_to(bundle)))
    manifest = {
        "target": {"os": "Windows", "arch": "x86_64", "python": "CPython 3.14", "wheel_platform": "win_amd64"},
        "route2": {"version": route2_version, "requirements": locked_file.name, "wheel": route2_wheel.name, "lock_sha256": _sha256(PROJECT / "uv.lock"), "offline_closure_verified": True},
        "datahub_lite": {"enabled": with_datahub_lite, "version": "1.7.0.12" if with_datahub_lite else None, "separate_environment_required": with_datahub_lite, "offline_closure_verified": with_datahub_lite},
        "files": [{"path": str(path.relative_to(bundle)), "size": path.stat().st_size, "sha256": _sha256(path)} for path in unique_files],
        "contains_business_data": False,
    }
    manifest_file = bundle / "sha256-manifest.json"
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"bundle": str(bundle), "wheels": len(list(wheelhouse.glob("*.whl"))), "route2_requirements": len(simple), "datahub_lite": with_datahub_lite, "manifest": str(manifest_file)}


def main():
    parser = argparse.ArgumentParser(description="Build an offline Windows CPython 3.14 wheel bundle")
    parser.add_argument("--bundle", required=True, type=Path, help="Output bundle directory")
    parser.add_argument("--cache", type=Path, default=PROJECT / ".cache/wheelhouse-win-py314", help="Existing wheel cache to reuse")
    parser.add_argument("--pip-python", type=Path, default=PROJECT / ".venv-datahub/bin/python", help="Preparation Python with pip")
    parser.add_argument("--with-datahub-lite", action="store_true", help="Include DataHub Lite wheels for a separate venv")
    parser.add_argument("--offline-only", action="store_true", help="Require all wheels to already exist in the local cache")
    args = parser.parse_args()
    print(json.dumps(prepare(args.bundle, args.cache, args.pip_python, with_datahub_lite=args.with_datahub_lite, offline_only=args.offline_only), ensure_ascii=False))


if __name__ == "__main__":
    main()
