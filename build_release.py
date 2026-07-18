from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from tools.audit_public_source import audit_source
from tools.verify_release import verify_release

ROOT = Path(__file__).resolve().parent
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
PRODUCT_BASENAME = "PhotographerImageArchive"
PACKAGE_ROOT_NAME = f"{PRODUCT_BASENAME}-{VERSION}-windows-x64"
PUBLIC_DOCUMENTS = ("README.md", "LICENSE", "CHANGELOG.md", "PRIVACY.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md")
RELEASE_PYTHON_VERSION = (3, 12, 13)


def _remove_generated(path: Path) -> None:
    resolved = path.resolve()
    if ROOT not in resolved.parents:
        raise RuntimeError(f"refusing to remove path outside repository: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def _release_entry_bytes(path: Path, source_dir: Path) -> bytes:
    relative = path.relative_to(source_dir)
    data = path.read_bytes()
    if relative.name in PUBLIC_DOCUMENTS:
        text = data.decode("utf-8")
        return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    return data


def _write_deterministic_zip(source_dir: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(item for item in source_dir.rglob("*") if item.is_file()):
            relative = Path(PACKAGE_ROOT_NAME) / path.relative_to(source_dir)
            info = zipfile.ZipInfo(relative.as_posix(), date_time=(2026, 7, 18, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(
                info,
                _release_entry_bytes(path, source_dir),
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )


def build() -> tuple[Path, Path]:
    if os.name != "nt":
        raise RuntimeError("The Windows release must be built on Windows.")
    if tuple(sys.version_info[:3]) != RELEASE_PYTHON_VERSION:
        expected = ".".join(str(part) for part in RELEASE_PYTHON_VERSION)
        raise RuntimeError(f"The Windows release requires Python {expected} exactly.")
    violations = audit_source(ROOT)
    if violations:
        raise RuntimeError("public source audit failed:\n" + "\n".join(violations))

    build_root = ROOT / "build"
    dist_root = ROOT / "dist"
    _remove_generated(build_root)
    _remove_generated(dist_root)
    build_root.mkdir(parents=True)
    dist_root.mkdir(parents=True)

    env = os.environ.copy()
    env.update({"PYTHONHASHSEED": "0", "SOURCE_DATE_EPOCH": "1784332800"})
    conda_runtime_bin = Path(sys.prefix) / "Library" / "bin"
    if conda_runtime_bin.is_dir():
        env["PATH"] = os.pathsep.join((str(conda_runtime_bin), env.get("PATH", "")))
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            "--distpath",
            str(build_root / "pyinstaller-dist"),
            "--workpath",
            str(build_root / "pyinstaller-work"),
            str(ROOT / "PhotographerImageArchive.spec"),
        ],
        cwd=ROOT,
        env=env,
        check=True,
    )
    executable = build_root / "pyinstaller-dist" / f"{PRODUCT_BASENAME}.exe"
    if not executable.is_file():
        raise RuntimeError(f"PyInstaller did not produce {executable}")

    smoke = subprocess.run([str(executable), "--release-smoke"], cwd=ROOT, timeout=45, check=False)
    if smoke.returncode != 0:
        raise RuntimeError(f"packaged executable smoke test failed with exit code {smoke.returncode}")
    ui_smoke = subprocess.run([str(executable), "--ui-smoke"], cwd=ROOT, timeout=45, check=False)
    if ui_smoke.returncode != 0:
        raise RuntimeError(f"packaged UI smoke test failed with exit code {ui_smoke.returncode}")

    stage = build_root / "release-stage"
    stage.mkdir()
    shutil.copy2(executable, stage / executable.name)
    for name in PUBLIC_DOCUMENTS:
        shutil.copy2(ROOT / name, stage / name)
    shutil.copytree(ROOT / "THIRD_PARTY_LICENSES", stage / "THIRD_PARTY_LICENSES")

    zip_path = dist_root / f"{PACKAGE_ROOT_NAME}.zip"
    _write_deterministic_zip(stage, zip_path)
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    checksum_path = dist_root / f"SHA256SUMS-v{VERSION}.txt"
    checksum_path.write_text(f"{digest}  {zip_path.name}\n", encoding="utf-8", newline="\n")
    verify_release(zip_path, checksum_path, VERSION)
    return zip_path, checksum_path


if __name__ == "__main__":
    package, checksums = build()
    print(package)
    print(checksums)
