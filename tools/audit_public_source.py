from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

IGNORED_PARTS = {".git", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__", "build", "dist"}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log", ".zip", ".7z", ".rar", ".exe", ".part"}
IMAGE_SUFFIXES = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
SCREENSHOT_NAMES = re.compile(r"(?i)(screen[-_ ]?shot|capture|review[-_ ]?image|desktop[-_ ]?view)")
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
GENERIC_USER_PATH_PATTERNS = (
    re.compile(rb"(?i)[A-Z]:(?:\\+|/)Users(?:\\+|/)[A-Za-z0-9._-]+(?:\\+|/)"),
    re.compile(rb"(?i)/(?:Users|home)/[A-Za-z0-9._-]+/"),
)
GENERIC_USER_TEXT_PATH_PATTERNS = (
    re.compile(r"(?i)[A-Z]:(?:\\+|/)Users(?:\\+|/)[A-Za-z0-9._-]+(?:\\+|/)"),
    re.compile(r"(?i)/(?:Users|home)/[A-Za-z0-9._-]+/"),
)
WIDE_TEXT_ENCODINGS = (("utf-16-le", 2), ("utf-16-be", 2), ("utf-32-le", 4), ("utf-32-be", 4))


class SourceAuditError(RuntimeError):
    pass


def _is_ignored(path: Path, root: Path) -> bool:
    return any(part in IGNORED_PARTS or part.startswith(".venv") for part in path.relative_to(root).parts)


def _sensitive_needles() -> tuple[bytes, ...]:
    home = Path.home().resolve()
    candidates = {str(home), home.as_posix()}
    username = os.environ.get("USERNAME", "").strip()
    if username:
        candidates.update({f"Users/{username}/", f"Users\\{username}\\"})
    needles: set[bytes] = set()
    for value in candidates:
        if not value:
            continue
        needles.add(value.encode("utf-8", errors="ignore"))
        for encoding, _alignment in WIDE_TEXT_ENCODINGS:
            needles.add(value.encode(encoding, errors="ignore"))
    return tuple(needle for needle in needles if needle)


def _wide_text_views(data: bytes):
    for encoding, alignment in WIDE_TEXT_ENCODINGS:
        for offset in range(alignment):
            yield data[offset:].decode(encoding, errors="ignore")


def audit_source(root: Path) -> list[str]:
    root = root.resolve()
    violations: list[str] = []
    needles = _sensitive_needles()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if _is_ignored(path, root):
            continue
        relative = path.relative_to(root).as_posix()
        suffix = path.suffix.casefold()
        if suffix in FORBIDDEN_SUFFIXES:
            violations.append(f"forbidden tracked artifact: {relative}")
            continue
        if SCREENSHOT_NAMES.search(path.name):
            violations.append(f"screenshot-like file is not public-release material: {relative}")
        if suffix in IMAGE_SUFFIXES and not relative.startswith("assets/photo_archive_icons/"):
            violations.append(f"non-icon image is not allowed in public source: {relative}")
        data = path.read_bytes()
        if (
            any(needle in data for needle in needles)
            or any(pattern.search(data) for pattern in GENERIC_USER_PATH_PATTERNS)
            or any(pattern.search(text) for text in _wide_text_views(data) for pattern in GENERIC_USER_TEXT_PATH_PATTERNS)
        ):
            violations.append(f"machine-specific home path found: {relative}")
        if any(pattern.search(data) for pattern in SECRET_PATTERNS) or any(
            pattern.search(text) for text in _wide_text_views(data) for pattern in TEXT_SECRET_PATTERNS
        ):
            violations.append(f"credential-like content found: {relative}")
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reject private or machine-specific files before publication.")
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    violations = audit_source(args.root)
    if violations:
        for violation in violations:
            print(f"ERROR: {violation}")
        return 1
    print("Public source audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
