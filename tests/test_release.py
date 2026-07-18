from __future__ import annotations

import hashlib
import re
import sys
import zipfile
from importlib.metadata import distribution
from pathlib import Path

import pytest

from build_release import _write_deterministic_zip
from tools.audit_public_source import audit_source
from tools.verify_release import THIRD_PARTY_LICENSE_FILES, ReleaseVerificationError, verify_release

VERSION = "1.0.0"
PACKAGE_ROOT = f"PhotographerImageArchive-{VERSION}-windows-x64"
WIDE_ENCODINGS = (("utf-16-le", 2), ("utf-16-be", 2), ("utf-32-le", 4), ("utf-32-be", 4))
EXPECTED_FILES = {
    "PhotographerImageArchive.exe": b"MZ public executable",
    "README.md": b"readme",
    "LICENSE": b"license",
    "CHANGELOG.md": b"changes",
    "PRIVACY.md": b"privacy",
    "SECURITY.md": b"security",
    "THIRD_PARTY_NOTICES.md": b"notices",
}
EXPECTED_FILES.update(
    {f"THIRD_PARTY_LICENSES/{name}": f"license for {name}".encode() for name in THIRD_PARTY_LICENSE_FILES}
)
RUNTIME_LICENSE_HASHES = {
    "BZIP2.txt": "c6dbbf828498be844a89eaa3b84adbab3199e342eb5cb2ed2f0d4ba7ec0f38a3",
    "LIBFFI.txt": "67894089811f93fca47a76f85e017da6f8582d4ba0905963c6e0f1ad6df7a195",
    "PYINSTALLER.txt": "dcf75fdb959db1e3b41c0f8505069d2ece781b5ec6b3d0a4d30975cfc6580245",
    "TCL-TK.txt": "2cde822b93ca16ae535c954b7dfe658b4ad10df2a193628d1b358f1765e8b198",
    "XZ.txt": "616a3ad264ce29b8f1cb97e53037b139d406899ca8d1f799651e17bfa09830b8",
    "XZ-0BSD.txt": "0b01625d853911cd0e2e088dcfb743261034a091bb379246cb25a14cc4c74bf1",
    "XZ-GPL-2.0.txt": "edaef632cbb643e4e7a221717a6c441a4c1a7c918e6e4d56debc3d8739b233f6",
    "ZLIB.txt": "e32ff4e00d9d94930537635291da39e7e612703334bf6fde8c7f1686fe8a45a2",
}


def _package(tmp_path: Path, files: dict[str, bytes] | None = None) -> tuple[Path, Path]:
    zip_path = tmp_path / f"{PACKAGE_ROOT}.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for name, content in (files or EXPECTED_FILES).items():
            archive.writestr(f"{PACKAGE_ROOT}/{name}", content)
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    checksum = tmp_path / f"SHA256SUMS-v{VERSION}.txt"
    checksum.write_text(f"{digest}  {zip_path.name}\n", encoding="utf-8")
    return zip_path, checksum


def test_release_verifier_accepts_exact_public_manifest(tmp_path: Path) -> None:
    zip_path, checksum = _package(tmp_path)
    verify_release(zip_path, checksum, VERSION)


@pytest.mark.parametrize("name", ["catalog.db", "activity.log", "local-screenshot.png"])
def test_release_verifier_rejects_private_artifacts(tmp_path: Path, name: str) -> None:
    files = dict(EXPECTED_FILES)
    files[name] = b"private"
    zip_path, checksum = _package(tmp_path, files)
    with pytest.raises(ReleaseVerificationError):
        verify_release(zip_path, checksum, VERSION)


def test_release_verifier_rejects_embedded_user_path(tmp_path: Path) -> None:
    files = dict(EXPECTED_FILES)
    local_path = b"C:" + b"\\Users\\" + b"PrivateDeveloper\\Documents\\archive.db"
    files["PhotographerImageArchive.exe"] += local_path
    zip_path, checksum = _package(tmp_path, files)
    with pytest.raises(ReleaseVerificationError, match="absolute user path"):
        verify_release(zip_path, checksum, VERSION)


def test_release_verifier_rejects_noncanonical_text_line_endings(tmp_path: Path) -> None:
    files = dict(EXPECTED_FILES)
    files["README.md"] = b"first line\r\nsecond line\r\n"
    zip_path, checksum = _package(tmp_path, files)

    with pytest.raises(ReleaseVerificationError, match="non-canonical text line endings"):
        verify_release(zip_path, checksum, VERSION)


@pytest.mark.parametrize(("encoding", "alignment"), WIDE_ENCODINGS)
def test_release_verifier_rejects_wide_user_path(tmp_path: Path, encoding: str, alignment: int) -> None:
    local_path = ("C:" + "\\Users\\" + "PrivateDeveloper\\Pictures\\archive.db").encode(encoding)
    for offset in range(alignment):
        files = dict(EXPECTED_FILES)
        files["PhotographerImageArchive.exe"] += b"X" * offset + local_path
        zip_path, checksum = _package(tmp_path, files)
        with pytest.raises(ReleaseVerificationError, match="absolute user path"):
            verify_release(zip_path, checksum, VERSION)


@pytest.mark.parametrize(("encoding", "alignment"), WIDE_ENCODINGS)
def test_release_verifier_rejects_wide_credential(tmp_path: Path, encoding: str, alignment: int) -> None:
    token = ("ghp_" + "A" * 32).encode(encoding)
    for offset in range(alignment):
        files = dict(EXPECTED_FILES)
        files["PhotographerImageArchive.exe"] += b"X" * offset + token
        zip_path, checksum = _package(tmp_path, files)
        with pytest.raises(ReleaseVerificationError, match="credential-like"):
            verify_release(zip_path, checksum, VERSION)


def test_release_verifier_rejects_wrong_checksum(tmp_path: Path) -> None:
    zip_path, checksum = _package(tmp_path)
    checksum.write_text(f"{'0' * 64}  {zip_path.name}\n", encoding="utf-8")
    with pytest.raises(ReleaseVerificationError, match="checksum"):
        verify_release(zip_path, checksum, VERSION)


def test_source_audit_allows_only_icon_images(tmp_path: Path) -> None:
    (tmp_path / "assets" / "photo_archive_icons").mkdir(parents=True)
    (tmp_path / "assets" / "photo_archive_icons" / "icon.png").write_bytes(b"public icon")
    assert audit_source(tmp_path) == []
    (tmp_path / "review-screenshot.png").write_bytes(b"private")
    assert audit_source(tmp_path)


def test_source_audit_ignores_generated_virtual_environments(tmp_path: Path) -> None:
    generated = tmp_path / ".venv-release" / "Scripts"
    generated.mkdir(parents=True)
    (generated / "python.exe").write_bytes(b"generated runtime")

    assert audit_source(tmp_path) == []


def test_source_audit_rejects_another_developer_home_path(tmp_path: Path) -> None:
    private_path = "C:" + "\\Users\\" + "AnotherDeveloper\\Pictures\\archive.db"
    (tmp_path / "config.py").write_text(f"ARCHIVE = {private_path!r}\n", encoding="utf-8")

    assert audit_source(tmp_path)


@pytest.mark.parametrize(("encoding", "alignment"), WIDE_ENCODINGS)
def test_source_audit_rejects_wide_other_developer_path(tmp_path: Path, encoding: str, alignment: int) -> None:
    private_path = "C:" + "\\Users\\" + "AnotherDeveloper\\Pictures\\archive.db"
    for offset in range(alignment):
        (tmp_path / "resource.bin").write_bytes(b"X" * offset + private_path.encode(encoding))
        assert audit_source(tmp_path)


@pytest.mark.parametrize(("encoding", "alignment"), WIDE_ENCODINGS)
def test_source_audit_rejects_wide_credential(tmp_path: Path, encoding: str, alignment: int) -> None:
    token = ("ghp_" + "B" * 32).encode(encoding)
    for offset in range(alignment):
        (tmp_path / "resource.bin").write_bytes(b"X" * offset + token)
        assert audit_source(tmp_path)


def test_all_declared_third_party_licenses_are_tracked() -> None:
    root = Path(__file__).resolve().parents[1]
    license_dir = root / "THIRD_PARTY_LICENSES"

    assert {path.name for path in license_dir.iterdir() if path.is_file()} == THIRD_PARTY_LICENSE_FILES
    assert all((license_dir / name).stat().st_size > 20 for name in THIRD_PARTY_LICENSE_FILES)


def test_deterministic_zip_normalizes_public_text_line_endings(tmp_path: Path) -> None:
    lf_stage = tmp_path / "lf"
    crlf_stage = tmp_path / "crlf"
    for stage, newline in ((lf_stage, b"\n"), (crlf_stage, b"\r\n")):
        (stage / "THIRD_PARTY_LICENSES").mkdir(parents=True)
        (stage / "PhotographerImageArchive.exe").write_bytes(b"MZ stable executable")
        (stage / "README.md").write_bytes(newline.join((b"first", b"second", b"")))
        (stage / "THIRD_PARTY_LICENSES" / "EXAMPLE.txt").write_bytes(
            newline.join((b"license", b"notice", b""))
        )

    lf_zip = tmp_path / "lf.zip"
    crlf_zip = tmp_path / "crlf.zip"
    _write_deterministic_zip(lf_stage, lf_zip)
    _write_deterministic_zip(crlf_stage, crlf_zip)

    assert lf_zip.read_bytes() == crlf_zip.read_bytes()


def test_pillow_license_matches_locked_distribution_bytes() -> None:
    root = Path(__file__).resolve().parents[1]
    pillow = distribution("Pillow")
    license_entry = next(
        entry
        for entry in (pillow.files or ())
        if str(entry).replace("\\", "/").endswith(".dist-info/licenses/LICENSE")
    )

    assert (root / "THIRD_PARTY_LICENSES" / "PILLOW.txt").read_bytes() == Path(
        pillow.locate_file(license_entry)
    ).read_bytes()


@pytest.mark.parametrize(("name", "expected_hash"), sorted(RUNTIME_LICENSE_HASHES.items()))
def test_runtime_license_bytes_match_locked_components(name: str, expected_hash: str) -> None:
    root = Path(__file__).resolve().parents[1]
    data = (root / "THIRD_PARTY_LICENSES" / name).read_bytes()

    assert hashlib.sha256(data).hexdigest() == expected_hash


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("PYINSTALLER.txt", Path("Lib/site-packages/pyinstaller-6.21.0.dist-info/licenses/COPYING.txt")),
        ("TCL-TK.txt", Path("Library/lib/tk8.6/license.terms")),
        ("XZ.txt", Path("Library/share/doc/xz/COPYING")),
        ("XZ-0BSD.txt", Path("Library/share/doc/xz/COPYING.0BSD")),
        ("XZ-GPL-2.0.txt", Path("Library/share/doc/xz/COPYING.GPLv2")),
        ("ZLIB.txt", Path("Library/share/doc/zlib/zlib/LICENSE")),
    ],
)
def test_runtime_license_matches_locked_environment_bytes(name: str, source: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    environment_source = Path(sys.prefix) / source

    if not environment_source.is_file():
        pytest.skip("exact runtime license source is available in the locked Windows release environment")
    assert (root / "THIRD_PARTY_LICENSES" / name).read_bytes() == environment_source.read_bytes()


def test_release_workflow_pins_actions_and_limits_write_permission() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "windows-release.yml").read_text(encoding="utf-8")
    action_refs = re.findall(r"uses:\s+[^@\s]+@([^\s#]+)", workflow)

    assert action_refs
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs)
    assert workflow.count("contents: write") == 1
    assert workflow.index("publish-release:") < workflow.index("contents: write")
    assert f"dist/PhotographerImageArchive-{VERSION}-windows-x64.zip" in workflow
    assert f"dist/SHA256SUMS-v{VERSION}.txt" in workflow
    assert "dist/PhotographerImageArchive-*" not in workflow
    assert "dist/SHA256SUMS-v*" not in workflow
    assert "runs-on: windows-2025" in workflow
    assert "windows-latest" not in workflow
    assert "runs-on: ubuntu-24.04" in workflow
    assert "ubuntu-latest" not in workflow
    assert "setup-python" not in workflow
    assert "conda-incubator/setup-miniconda" not in workflow
    assert "Miniconda3-py312_25.5.1-1-Windows-x86_64.exe" in workflow
    assert "0905638dcd7bb214261fe0ac6136d4dbbb9c4266fa3cf4c5bf4ffce834ae5357" in workflow
    assert "tools\\create_locked_conda_env.ps1" in workflow
    assert ".venv-release\\python.exe" in workflow


def test_windows_runtime_lock_uses_exact_artifacts() -> None:
    root = Path(__file__).resolve().parents[1]
    bootstrap = (root / "tools" / "create_locked_conda_env.ps1").read_text(encoding="utf-8")
    lines = [
        line.strip()
        for line in (root / "environment-release-win-64.lock").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]

    assert lines[0] == "@EXPLICIT"
    assert len(lines) == 21
    assert all(re.fullmatch(r"https://repo\.anaconda\.com/.+\.conda#[0-9a-f]{64}", line) for line in lines[1:])
    assert any(
        "/python-3.12.13-hd7b1df3_3.conda#6c6862152ca12711eaf2efb6dbfd50cdf773266ac63ab630fc7ef905ab955ac5"
        in line
        for line in lines
    )
    assert "environment-release-win-64.lock" in bootstrap
    assert "Algorithm SHA256" in bootstrap


def test_repository_enforces_lf_for_public_text() -> None:
    root = Path(__file__).resolve().parents[1]
    attributes = (root / ".gitattributes").read_text(encoding="utf-8")

    assert "* text=auto eol=lf" in attributes
    assert "THIRD_PARTY_LICENSES/** -text -diff" in attributes
