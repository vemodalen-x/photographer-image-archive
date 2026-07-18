from __future__ import annotations

import argparse
import hashlib
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath

FORBIDDEN_SUFFIXES = {
    ".bmp",
    ".db",
    ".gif",
    ".jpeg",
    ".jpg",
    ".log",
    ".png",
    ".sqlite",
    ".sqlite3",
    ".tif",
    ".tiff",
    ".webp",
}
SCREENSHOT_NAMES = re.compile(r"(?i)(screen[-_ ]?shot|capture|review[-_ ]?image|desktop[-_ ]?view)")
PATH_PATTERNS = (
    re.compile(rb"(?i)[A-Z]:(?:\\+|/)Users(?:\\+|/)[A-Za-z0-9._-]+(?:\\+|/)"),
    re.compile(rb"(?i)/(?:Users|home)/[A-Za-z0-9._-]+/"),
)
TEXT_PATH_PATTERNS = (
    re.compile(r"(?i)[A-Z]:(?:\\+|/)Users(?:\\+|/)[A-Za-z0-9._-]+(?:\\+|/)"),
    re.compile(r"(?i)/(?:Users|home)/[A-Za-z0-9._-]+/"),
)
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"ghp_[A-Za-z0-9]{20,}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
)
TEXT_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
)
WIDE_TEXT_ENCODINGS = (("utf-16-le", 2), ("utf-16-be", 2), ("utf-32-le", 4), ("utf-32-be", 4))
THIRD_PARTY_LICENSE_FILES = {
    "BZIP2.txt",
    "CERTIFI.txt",
    "CHARSET_NORMALIZER.txt",
    "EXPAT.txt",
    "IDNA.txt",
    "LIBFFI.txt",
    "LUCIDE.txt",
    "OPENSSL.txt",
    "PILLOW.txt",
    "PYINSTALLER.txt",
    "PYTHON.txt",
    "REQUESTS-NOTICE.txt",
    "REQUESTS.txt",
    "SQLITE.txt",
    "TCL-TK.txt",
    "TYPING_EXTENSIONS.txt",
    "URLLIB3.txt",
    "XZ.txt",
    "XZ-0BSD.txt",
    "XZ-GPL-2.0.txt",
    "ZLIB.txt",
}


class ReleaseVerificationError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wide_text_views(data: bytes):
    for encoding, alignment in WIDE_TEXT_ENCODINGS:
        for offset in range(alignment):
            yield data[offset:].decode(encoding, errors="ignore")


def _expected_entries(version: str) -> set[str]:
    root = f"PhotographerImageArchive-{version}-windows-x64"
    entries = {
        f"{root}/PhotographerImageArchive.exe",
        f"{root}/README.md",
        f"{root}/LICENSE",
        f"{root}/CHANGELOG.md",
        f"{root}/PRIVACY.md",
        f"{root}/SECURITY.md",
        f"{root}/THIRD_PARTY_NOTICES.md",
    }
    entries.update(f"{root}/THIRD_PARTY_LICENSES/{name}" for name in THIRD_PARTY_LICENSE_FILES)
    return entries


def _check_entry_name(name: str) -> None:
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or ".." in path.parts:
        raise ReleaseVerificationError(f"unsafe ZIP entry: {name!r}")
    if SCREENSHOT_NAMES.search(path.name) or path.suffix.casefold() in FORBIDDEN_SUFFIXES:
        raise ReleaseVerificationError(f"private visual/data artifact in ZIP: {name}")


def _check_content(name: str, data: bytes) -> None:
    preserve_raw_bytes = name.endswith("/PhotographerImageArchive.exe") or "/THIRD_PARTY_LICENSES/" in name
    if not preserve_raw_bytes and b"\r" in data:
        raise ReleaseVerificationError(f"non-canonical text line endings in {name}")
    if any(pattern.search(data) for pattern in PATH_PATTERNS) or any(
        pattern.search(text) for text in _wide_text_views(data) for pattern in TEXT_PATH_PATTERNS
    ):
        raise ReleaseVerificationError(f"absolute user path embedded in {name}")
    if any(pattern.search(data) for pattern in SECRET_PATTERNS) or any(
        pattern.search(text) for text in _wide_text_views(data) for pattern in TEXT_SECRET_PATTERNS
    ):
        raise ReleaseVerificationError(f"credential-like content embedded in {name}")


def verify_release(zip_path: Path, checksum_path: Path, version: str) -> None:
    zip_path = zip_path.resolve()
    checksum_path = checksum_path.resolve()
    expected_checksum_line = f"{sha256_file(zip_path)}  {zip_path.name}"
    lines = [line.strip() for line in checksum_path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if lines != [expected_checksum_line]:
        raise ReleaseVerificationError("checksum manifest does not exactly match the release ZIP")

    seen: set[str] = set()
    total_uncompressed = 0
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            _check_entry_name(info.filename)
            if info.filename in seen:
                raise ReleaseVerificationError(f"duplicate ZIP entry: {info.filename}")
            seen.add(info.filename)
            if info.flag_bits & 0x1:
                raise ReleaseVerificationError(f"encrypted ZIP entry: {info.filename}")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ReleaseVerificationError(f"symbolic link ZIP entry: {info.filename}")
            if info.is_dir():
                raise ReleaseVerificationError(f"unexpected directory entry: {info.filename}")
            total_uncompressed += info.file_size
            if info.file_size > 256 * 1024 * 1024 or total_uncompressed > 300 * 1024 * 1024:
                raise ReleaseVerificationError("release ZIP exceeds the uncompressed size limit")
            _check_content(info.filename, archive.read(info))

    expected = _expected_entries(version)
    if seen != expected:
        missing = sorted(expected - seen)
        extra = sorted(seen - expected)
        raise ReleaseVerificationError(f"unexpected package manifest; missing={missing}, extra={extra}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the public Windows release ZIP and checksum.")
    parser.add_argument("zip_path", type=Path)
    parser.add_argument("checksum_path", type=Path)
    parser.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    try:
        verify_release(args.zip_path, args.checksum_path, args.version)
    except (OSError, ValueError, zipfile.BadZipFile, ReleaseVerificationError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"Release verification passed: {args.zip_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
