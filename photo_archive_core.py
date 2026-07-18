from __future__ import annotations

import datetime as dt
import hashlib
import heapq
import html
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from threading import Event, Thread
from typing import Callable, Iterable, Optional
from urllib.parse import unquote, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests

from photo_archive_version import __version__

try:
    from PIL import ExifTags, Image, ImageDraw, ImageFont, ImageOps
except ImportError:  # pragma: no cover - handled at runtime.
    ExifTags = None
    Image = None
    ImageDraw = None
    ImageFont = None
    ImageOps = None


USER_AGENT = f"PhotographerImageArchive/{__version__} (local research and rights-aware archival tool)"
COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"
WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
DEFAULT_OUTPUT_DIR = Path.home() / "Downloads" / "Photo Archive"
DEFAULT_MIN_LONG_EDGE = 1080
DEFAULT_NEAR_DUPLICATE_DISTANCE = 6
API_RETRIES = 2
OFFICIAL_SITE_MIN_SCORE = 80
COMMONS_MIN_MATCH_SCORE = 70
MAX_WEBSITE_PAGES = 96
MAX_SITEMAP_URLS = 160
MAX_RENDERED_PAGES = 32
BROWSER_RENDER_TIMEOUT_SECONDS = 28
BROWSER_VIRTUAL_TIME_BUDGET_MS = 10_000
MAX_RENDERED_DOM_CHARS = 16 * 1024 * 1024
MAX_IMAGE_DOWNLOAD_BYTES = 512 * 1024 * 1024
MAX_WEBSITE_HTML_BYTES = 8 * 1024 * 1024
MAX_ROBOTS_BYTES = 1024 * 1024
MAX_SITEMAP_BYTES = 8 * 1024 * 1024
MAX_QUEUED_PAGES = 512
MAX_PAGE_LINKS = 4096
MAX_PAGE_CANDIDATES = 4096
MAX_SAME_SITE_REDIRECTS = 5

ProgressCallback = Callable[[str, dict], None]


def _bounded_response_content(response, max_bytes: int, label: str) -> bytes:
    declared_size = _safe_int(response.headers.get("Content-Length"))
    if declared_size > max_bytes:
        raise PhotoArchiveError(f"{label} exceeds the {max_bytes // (1024 * 1024)} MiB response limit")

    iter_content = getattr(response, "iter_content", None)
    if callable(iter_content):
        chunks: list[bytes] = []
        total = 0
        for chunk in iter_content(chunk_size=256 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise PhotoArchiveError(f"{label} exceeds the {max_bytes // (1024 * 1024)} MiB response limit")
            chunks.append(chunk)
        return b"".join(chunks)

    content = getattr(response, "content", None)
    if content is None or (not content and getattr(response, "text", "")):
        text = str(getattr(response, "text", ""))
        content = text.encode(getattr(response, "encoding", None) or "utf-8", errors="replace")
    if len(content) > max_bytes:
        raise PhotoArchiveError(f"{label} exceeds the {max_bytes // (1024 * 1024)} MiB response limit")
    return bytes(content)


class PhotoArchiveError(Exception):
    """Base error for image discovery, download, and archive operations."""


class SearchCancelled(PhotoArchiveError):
    """Raised when a caller requests a clean stop of discovery or download."""


@dataclass
class PhotoRecord:
    source: str
    source_id: str
    search_name: str
    title: str
    page_url: str
    image_url: str
    collection_title: str = ""
    thumb_url: str = ""
    match_confidence: int = 0
    match_reason: str = ""
    width: int = 0
    height: int = 0
    mime: str = ""
    file_size: int = 0
    author: str = ""
    license_name: str = ""
    license_url: str = ""
    credit: str = ""
    annotation: str = ""
    source_comment: str = ""
    research_note: str = ""
    research_tags: str = ""
    rating: int = 0
    shooting_date: str = ""
    camera_make: str = ""
    camera_model: str = ""
    lens_model: str = ""
    exposure_time: str = ""
    f_number: str = ""
    iso: str = ""
    focal_length: str = ""
    raw_metadata_json: str = "{}"
    local_path: str = ""
    sha256: str = ""
    dhash: str = ""
    duplicate_of: str = ""
    near_duplicate_of: str = ""
    duplicate_distance: int = -1
    downloaded_at: str = ""

    @property
    def long_edge(self) -> int:
        return max(int(self.width or 0), int(self.height or 0))

    @property
    def resolution(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return ""

    @property
    def source_key(self) -> str:
        return f"{self.source}:{self.source_id}"


@dataclass
class ArchiveSummary:
    search_name: str
    output_dir: Path
    database_path: Path
    found: int
    saved: int
    downloaded: int
    exact_duplicates: int
    near_duplicates: int
    skipped_low_resolution: int
    records: list[PhotoRecord]


def create_contact_sheet_pages(
    items: list[tuple[PhotoRecord, bytes | None]],
    destination: Path,
    photographer: str,
    *,
    columns: int = 4,
    rows: int = 3,
) -> list[Path]:
    """Render fixed-format JPEG contact-sheet pages while tolerating missing images."""

    if Image is None or ImageDraw is None or ImageFont is None or ImageOps is None:
        raise PhotoArchiveError("Pillow is required to export contact sheets")
    if columns < 1 or rows < 1:
        raise ValueError("columns and rows must be positive")
    if not items:
        raise ValueError("at least one contact-sheet item is required")

    destination = destination.with_suffix(".jpg")
    destination.parent.mkdir(parents=True, exist_ok=True)
    per_page = columns * rows
    page_count = (len(items) + per_page - 1) // per_page
    cell_width, cell_height = 320, 270
    page_width = columns * cell_width
    title_font = _contact_sheet_font(22, bold=True)
    meta_font = _contact_sheet_font(15)
    caption_font = _contact_sheet_font(13)
    output_paths: list[Path] = []

    for page_index in range(page_count):
        start = page_index * per_page
        page_items = items[start : start + per_page]
        used_rows = max(1, (len(page_items) + columns - 1) // columns)
        page_height = 76 + used_rows * cell_height + 34
        page = Image.new("RGB", (page_width, page_height), "#F4F5F6")
        draw = ImageDraw.Draw(page)
        draw.text((20, 14), photographer or "Photographic study", fill="#172027", font=title_font)
        draw.text(
            (20, 44),
            f"Photographic study contact sheet  |  {len(items)} works",
            fill="#5A6670",
            font=meta_font,
        )
        for local_index, (record, content) in enumerate(page_items):
            row, column = divmod(local_index, columns)
            x = column * cell_width
            y = 76 + row * cell_height
            draw.rectangle((x + 8, y + 8, x + cell_width - 8, y + cell_height - 8), fill="#FFFFFF")
            image_box = (x + 16, y + 16, x + cell_width - 16, y + 202)
            draw.rectangle(image_box, fill="#20272E")
            if content:
                try:
                    with Image.open(BytesIO(content)) as source:
                        preview = ImageOps.contain(source.convert("RGB"), (image_box[2] - image_box[0], image_box[3] - image_box[1]))
                    image_x = image_box[0] + (image_box[2] - image_box[0] - preview.width) // 2
                    image_y = image_box[1] + (image_box[3] - image_box[1] - preview.height) // 2
                    page.paste(preview, (image_x, image_y))
                except Exception:
                    draw.text((image_box[0] + 12, image_box[1] + 80), "Preview unavailable", fill="#CAD1D6", font=meta_font)
            else:
                draw.text((image_box[0] + 12, image_box[1] + 80), "Preview unavailable", fill="#CAD1D6", font=meta_font)
            title = _contact_sheet_text(record.title or "Untitled", 43)
            source = "Official website" if record.source == "website" else (record.source or "Source")
            draw.text((x + 16, y + 211), title, fill="#172027", font=caption_font)
            draw.text((x + 16, y + 232), f"{record.resolution or 'Size unverified'}  |  {source}", fill="#5A6670", font=caption_font)
            draw.text((x + 16, y + 251), f"Match {record.match_confidence or 0}%", fill="#126B5C", font=caption_font)
        page_label = f"Page {page_index + 1} / {page_count}"
        draw.text((page_width - 112, page_height - 26), page_label, fill="#5A6670", font=caption_font)
        if page_count == 1:
            page_path = destination
        else:
            page_path = destination.with_name(f"{destination.stem}_p{page_index + 1:02d}.jpg")
        page.save(page_path, format="JPEG", quality=91, optimize=True)
        output_paths.append(page_path)
    return output_paths


def _contact_sheet_font(size: int, *, bold: bool = False):
    candidates = [
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / ("msyhbd.ttc" if bold else "msyh.ttc"),
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / ("arialbd.ttf" if bold else "arial.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            try:
                return ImageFont.truetype(str(candidate), size)
            except OSError:
                continue
    return ImageFont.load_default()


def _contact_sheet_text(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def normalize_research_tags(value: str) -> str:
    tags: list[str] = []
    seen: set[str] = set()
    for raw_tag in re.split(r"[,，;；\n]+", value or ""):
        tag = " ".join(raw_tag.strip().lstrip("#").split())
        identity = tag.casefold()
        if tag and identity not in seen:
            seen.add(identity)
            tags.append(tag)
    return ", ".join(tags)


def render_research_markdown(record: PhotoRecord) -> str:
    """Render one work as a portable UTF-8 research record."""

    rating = max(0, min(5, int(record.rating or 0)))
    rating_text = f"{rating}/5" if rating else "未评分"
    source_url = record.page_url or record.image_url
    lines = [
        f"# {record.title or '未命名作品'}",
        "",
        f"- 摄影师：{record.search_name or record.author or '-'}",
        f"- 系列 / 项目：{record.collection_title or '-'}",
        f"- 评分：{rating_text}",
        f"- 标签：{record.research_tags or '-'}",
        f"- 尺寸：{record.resolution or '待确认'}",
        f"- 日期：{record.shooting_date or '-'}",
        f"- 来源：{source_url or '-'}",
        f"- 图片地址：{record.image_url or '-'}",
        f"- 许可：{record.license_name or '-'}",
        f"- 本地文件：{record.local_path or '-'}",
        f"- SHA-256：{record.sha256 or '-'}",
        "",
        "## 研究笔记",
        "",
        record.research_note or "-",
        "",
        "## 作品注解",
        "",
        record.annotation or "-",
        "",
        "## 来源说明",
        "",
        record.source_comment or record.credit or "-",
        "",
        "## 拍摄细节",
        "",
        f"- 相机：{' '.join(value for value in (record.camera_make, record.camera_model) if value) or '-'}",
        f"- 镜头：{record.lens_model or '-'}",
        f"- 曝光：{record.exposure_time or '-'}",
        f"- 光圈：{record.f_number or '-'}",
        f"- ISO：{record.iso or '-'}",
        f"- 焦距：{record.focal_length or '-'}",
        "",
    ]
    return "\n".join(lines)


PHOTO_COLUMNS = [
    "source",
    "source_id",
    "search_name",
    "title",
    "collection_title",
    "page_url",
    "image_url",
    "thumb_url",
    "match_confidence",
    "match_reason",
    "width",
    "height",
    "mime",
    "file_size",
    "author",
    "license_name",
    "license_url",
    "credit",
    "annotation",
    "source_comment",
    "research_note",
    "research_tags",
    "rating",
    "shooting_date",
    "camera_make",
    "camera_model",
    "lens_model",
    "exposure_time",
    "f_number",
    "iso",
    "focal_length",
    "raw_metadata_json",
    "local_path",
    "sha256",
    "dhash",
    "duplicate_of",
    "near_duplicate_of",
    "duplicate_distance",
    "downloaded_at",
]


def archive_photographer(
    search_name: str,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    website_url: str = "",
    limit: int = 40,
    download_limit: Optional[int] = None,
    min_long_edge: int = DEFAULT_MIN_LONG_EDGE,
    download: bool = True,
    near_duplicate_distance: int = DEFAULT_NEAR_DUPLICATE_DISTANCE,
    callback: Optional[ProgressCallback] = None,
    cancel_event: Optional[Event] = None,
) -> ArchiveSummary:
    """Resolve sources and persist every accepted record before optional downloads.

    The callback receives incremental records and recoverable errors. ``cancel_event``
    is checked between network pages and download chunks so partial work remains valid.
    @codex-comment approved
    """

    search_name = search_name.strip()
    if not search_name:
        raise PhotoArchiveError("Please enter a photographer name.")

    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    store = ArchiveStore(output_root / "photo_archive.db")
    effective_website_url = website_url.strip()
    if not effective_website_url:
        effective_website_url = discover_official_website(
            search_name,
            callback=callback,
            cancel_event=cancel_event,
        )

    saved_by_key: dict[str, PhotoRecord] = {}

    def source_callback(event: str, payload: dict) -> None:
        forwarded = dict(payload)
        if event == "record_found" and isinstance(payload.get("record"), dict):
            try:
                saved = store.upsert(PhotoRecord(**payload["record"]))
                saved_by_key[saved.source_key] = saved
                forwarded["record"] = asdict(saved)
                _emit(callback, "record_persisted", source_key=saved.source_key, title=saved.title)
            except SearchCancelled:
                raise
            except Exception as exc:
                _emit(
                    callback,
                    "record_error",
                    title=str(payload.get("title") or "unknown"),
                    message=f"metadata save failed: {exc}",
                )
        _emit(callback, event, **forwarded)

    if effective_website_url:
        source = WebsiteImageSource()
        _emit(callback, "status", message=f"Searching official website for {search_name}: {effective_website_url}")
        search_result = source.search(
            effective_website_url,
            search_name=search_name,
            limit=limit,
            min_long_edge=min_long_edge,
            callback=source_callback,
            cancel_event=cancel_event,
        )
        if not search_result.records:
            _check_cancel(cancel_event)
            _emit(callback, "status", message=f"No accepted images found on official website; falling back to Wikimedia Commons for {search_name}")
            source = WikimediaCommonsSource()
            search_result = source.search(
                search_name,
                limit=limit,
                min_long_edge=min_long_edge,
                callback=source_callback,
                cancel_event=cancel_event,
            )
    else:
        source = WikimediaCommonsSource()
        _emit(callback, "status", message=f"Searching Wikimedia Commons for {search_name}")
        search_result = source.search(
            search_name,
            limit=limit,
            min_long_edge=min_long_edge,
            callback=source_callback,
            cancel_event=cancel_event,
        )

    saved_records: list[PhotoRecord] = list(saved_by_key.values())
    for record in search_result.records:
        if record.source_key in saved_by_key:
            continue
        try:
            saved = store.upsert(record)
            saved_by_key[saved.source_key] = saved
            saved_records.append(saved)
        except Exception as exc:
            _emit(callback, "record_error", title=record.title, message=f"metadata save failed: {exc}")

    downloaded_records: list[PhotoRecord] = []
    if download:
        candidates = saved_records
        if download_limit is not None:
            candidates = candidates[: max(0, download_limit)]
        for index, record in enumerate(candidates, start=1):
            _check_cancel(cancel_event)
            _emit(
                callback,
                "download_progress",
                current=index,
                total=len(candidates),
                title=record.title,
            )
            try:
                downloaded_records.append(
                    download_record(
                        record,
                        output_root,
                        store,
                        near_duplicate_distance=near_duplicate_distance,
                        callback=callback,
                        cancel_event=cancel_event,
                    )
                )
            except SearchCancelled:
                raise
            except Exception as exc:
                _emit(
                    callback,
                    "download_error",
                    title=record.title,
                    message=str(exc),
                    current=index,
                    total=len(candidates),
                )
                continue

    final_records = store.list_records(search_name=search_name)
    exact_duplicates = sum(1 for item in final_records if item.duplicate_of)
    near_duplicates = sum(1 for item in final_records if item.near_duplicate_of)
    return ArchiveSummary(
        search_name=search_name,
        output_dir=output_root,
        database_path=store.db_path,
        found=search_result.found,
        saved=len(saved_records),
        downloaded=len([item for item in downloaded_records if item.local_path]),
        exact_duplicates=exact_duplicates,
        near_duplicates=near_duplicates,
        skipped_low_resolution=search_result.skipped_low_resolution,
        records=final_records,
    )


@dataclass
class SearchResult:
    records: list[PhotoRecord]
    found: int
    skipped_low_resolution: int
    rejected_irrelevant: int = 0
    unknown_dimensions: int = 0
    duplicate_candidates: int = 0
    page_errors: int = 0
    pages_scanned: int = 0
    series_count: int = 0
    declared_total: int = 0


def discover_official_website(
    search_name: str,
    callback: Optional[ProgressCallback] = None,
    session: Optional[requests.Session] = None,
    cancel_event: Optional[Event] = None,
) -> str:
    """Return an official URL only when Wikidata strongly identifies a photographer.

    Candidate labels and aliases may be multilingual, but occupation or a photography
    description is mandatory. Low-confidence namesakes are reported and rejected.
    @codex-comment approved
    """

    session = session or requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    _emit(callback, "official_site_search", message=f"Looking up official website for {search_name}")
    candidates: dict[str, dict] = {}
    for language in ("zh", "en", "ja", "fr", "de"):
        _check_cancel(cancel_event)
        try:
            response = session.get(
                WIKIDATA_API_URL,
                params={
                    "action": "wbsearchentities",
                    "format": "json",
                    "language": language,
                    "uselang": language,
                    "search": search_name,
                    "limit": 5,
                },
                timeout=(8, 20),
            )
            response.raise_for_status()
            for item in response.json().get("search", []):
                entity_id = item.get("id")
                if not entity_id:
                    continue
                current = candidates.setdefault(entity_id, {"id": entity_id, "labels": [], "descriptions": []})
                current["labels"].append(str(item.get("label") or ""))
                current["descriptions"].append(str(item.get("description") or ""))
        except Exception as exc:
            _emit(callback, "source_retry", message=f"Official-site lookup failed for {language}: {exc}")

    if not candidates:
        _emit(callback, "official_site_missing", message=f"No official website candidate found for {search_name}")
        return ""

    _check_cancel(cancel_event)
    try:
        response = session.get(
            WIKIDATA_API_URL,
            params={
                "action": "wbgetentities",
                "format": "json",
                "ids": "|".join(candidates),
                "props": "claims|labels|aliases|descriptions",
                "languages": "zh|en|ja|fr|de",
            },
            timeout=(8, 20),
        )
        response.raise_for_status()
        entities = response.json().get("entities", {})
    except Exception as exc:
        _emit(callback, "official_site_missing", message=f"Could not read official website data: {exc}")
        return ""

    ranked: list[tuple[int, str, str, str]] = []
    for entity_id, entity in entities.items():
        _check_cancel(cancel_event)
        claims = entity.get("claims", {})
        websites = [
            claim.get("mainsnak", {}).get("datavalue", {}).get("value", "")
            for claim in claims.get("P856", [])
        ]
        websites = [url for url in websites if isinstance(url, str) and url.startswith(("http://", "https://"))]
        if not websites:
            continue
        label = _entity_best_text(entity.get("labels", {}), candidates.get(entity_id, {}).get("labels", []))
        description = _entity_best_text(entity.get("descriptions", {}), candidates.get(entity_id, {}).get("descriptions", []))
        aliases = _entity_aliases(entity.get("aliases", {}))
        score = _official_site_score(search_name, label, description, claims, aliases=aliases)
        ranked.append((score, websites[0], label, description))

    if not ranked:
        _emit(callback, "official_site_missing", message=f"No official website field found for {search_name}")
        return ""
    ranked.sort(key=lambda item: item[0], reverse=True)
    score, url, label, description = ranked[0]
    if score < OFFICIAL_SITE_MIN_SCORE:
        _emit(
            callback,
            "official_site_rejected",
            url=url,
            label=label,
            description=description,
            score=score,
            message=f"Rejected low-confidence website match for {search_name}: {label or url}",
        )
        _emit(callback, "official_site_missing", message=f"No confident official website found for {search_name}")
        return ""
    _emit(callback, "official_site_found", url=url, label=label, description=description, score=score)
    return url


def _entity_best_text(values: dict, fallback: list[str]) -> str:
    for language in ("zh", "en", "ja", "fr", "de"):
        value = values.get(language, {}).get("value") if isinstance(values.get(language), dict) else ""
        if value:
            return str(value)
    return next((item for item in fallback if item), "")


def _entity_aliases(values: dict) -> list[str]:
    aliases: list[str] = []
    for payloads in values.values():
        if not isinstance(payloads, list):
            continue
        aliases.extend(str(item.get("value") or "") for item in payloads if isinstance(item, dict))
    return [alias for alias in aliases if alias]


def _official_site_score(
    search_name: str,
    label: str,
    description: str,
    claims: dict,
    aliases: Iterable[str] = (),
) -> int:
    score = 0
    normalized_query = _normalize_person_name(search_name)
    normalized_names = {_normalize_person_name(value) for value in (label, *aliases) if value}
    if normalized_query and normalized_query in normalized_names:
        score += 40
    elif normalized_query and any(normalized_query in value or value in normalized_query for value in normalized_names if value):
        score += 20
    description_lower = description.casefold()
    if any(token in description_lower for token in ("photographer", "摄影", "写真家", "photographe", "fotograf")):
        score += 40
    occupation_ids = {
        claim.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id")
        for claim in claims.get("P106", [])
    }
    if "Q33231" in occupation_ids:
        score += 60
    instance_ids = {
        claim.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id")
        for claim in claims.get("P31", [])
    }
    if "Q5" in instance_ids:
        score += 10
    return score


@dataclass
class WebsiteImageCandidate:
    image_url: str
    thumb_url: str
    page_url: str
    title: str
    collection_title: str = ""
    alt: str = ""
    width: int = 0
    height: int = 0
    dimensions_verified: bool = False


def _terminate_process(process) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


class BrowserDOMRenderer:
    """Render public JavaScript pages in an isolated local Chromium profile.

    The renderer never imports a user profile, cookies, extensions, or credentials and
    keeps Chromium's sandbox enabled. It is a bounded fallback for otherwise empty
    public gallery pages, not an access-control bypass. @codex-comment approved
    """

    def __init__(self, executable: Path | str | None = None) -> None:
        self.executable = Path(executable) if executable else _find_chromium_executable()

    @property
    def available(self) -> bool:
        return bool(self.executable and self.executable.exists())

    def build_command(self, profile_dir: Path, url: str) -> list[str]:
        if not self.executable:
            return []
        return [
            str(self.executable),
            "--headless=new",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-sync",
            "--disable-background-networking",
            "--disable-component-update",
            "--log-level=3",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={profile_dir}",
            f"--virtual-time-budget={BROWSER_VIRTUAL_TIME_BUDGET_MS}",
            "--dump-dom",
            url,
        ]

    def render(
        self,
        url: str,
        callback: Optional[ProgressCallback] = None,
        cancel_event: Optional[Event] = None,
    ) -> str:
        if not self.available:
            return ""
        _check_cancel(cancel_event)
        _emit(callback, "source_render", message=f"Rendering dynamic public page: {url}", url=url)
        with tempfile.TemporaryDirectory(prefix="photo_archive_browser_", ignore_cleanup_errors=True) as profile:
            command = self.build_command(Path(profile), url)
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            dom_path = Path(profile) / "rendered-dom.html"
            error_path = Path(profile) / "renderer-error.log"
            with error_path.open("wb") as error_handle:
                try:
                    process = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=error_handle,
                        creationflags=creationflags,
                    )
                except OSError as exc:
                    _emit(callback, "source_notice", message=f"Dynamic renderer unavailable: {exc}")
                    return ""

                limit_exceeded = Event()
                reader_errors: list[Exception] = []

                def capture_stdout() -> None:
                    total = 0
                    try:
                        if process.stdout is None:
                            return
                        with dom_path.open("wb") as dom_handle:
                            while True:
                                chunk = process.stdout.read(256 * 1024)
                                if not chunk:
                                    break
                                remaining = MAX_RENDERED_DOM_CHARS - total
                                if len(chunk) > remaining:
                                    if remaining > 0:
                                        dom_handle.write(chunk[:remaining])
                                    limit_exceeded.set()
                                    break
                                dom_handle.write(chunk)
                                total += len(chunk)
                    except Exception as exc:
                        reader_errors.append(exc)

                reader = Thread(target=capture_stdout, name="photo-archive-dom-reader", daemon=True)
                reader.start()

                deadline = time.monotonic() + BROWSER_RENDER_TIMEOUT_SECONDS
                while True:
                    if limit_exceeded.is_set():
                        _terminate_process(process)
                        reader.join(timeout=2)
                        _emit(
                            callback,
                            "source_notice",
                            message=f"Dynamic page exceeded the {MAX_RENDERED_DOM_CHARS // (1024 * 1024)} MB DOM limit and was skipped: {url}",
                        )
                        return ""
                    try:
                        process.wait(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        if cancel_event is not None and cancel_event.is_set():
                            _terminate_process(process)
                            reader.join(timeout=2)
                            raise SearchCancelled("Search cancelled by user.")
                        if time.monotonic() >= deadline:
                            _terminate_process(process)
                            reader.join(timeout=2)
                            _emit(callback, "source_notice", message=f"Dynamic rendering timed out: {url}")
                            return ""

                reader.join(timeout=2)
                if reader.is_alive():
                    _terminate_process(process)
                    _emit(callback, "source_notice", message=f"Dynamic rendering output did not close: {url}")
                    return ""
                if reader_errors:
                    _emit(callback, "source_notice", message=f"Dynamic rendering output failed: {reader_errors[0]}")
                    return ""
                if limit_exceeded.is_set():
                    _emit(
                        callback,
                        "source_notice",
                        message=f"Dynamic page exceeded the {MAX_RENDERED_DOM_CHARS // (1024 * 1024)} MB DOM limit and was skipped: {url}",
                    )
                    return ""

            if process.returncode != 0:
                detail = html_to_text(error_path.read_bytes()[-4096:].decode("utf-8", errors="replace"))[-240:]
                _emit(callback, "source_notice", message=f"Dynamic rendering failed: {detail or process.returncode}")
                return ""
            stdout = dom_path.read_text(encoding="utf-8", errors="replace")
            return stdout if "<" in stdout else ""


class WebsiteImageSource:
    source_name = "website"

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        renderer: Optional[BrowserDOMRenderer] = None,
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.renderer = renderer or BrowserDOMRenderer()

    def search(
        self,
        website_url: str,
        search_name: str,
        limit: int = 40,
        min_long_edge: int = DEFAULT_MIN_LONG_EDGE,
        callback: Optional[ProgressCallback] = None,
        cancel_event: Optional[Event] = None,
    ) -> SearchResult:
        """Crawl an official site using robots, sitemap hints, and ranked page links.

        Public HTML is parsed incrementally; login flows and blocked paths are never
        followed. Portfolio/project URLs are preferred over news, shop, and legal pages.
        @codex-comment approved
        """

        start_url = _normalize_url(website_url)
        start_host = urlparse(start_url).netloc.lower()
        records: list[PhotoRecord] = []
        skipped_low_resolution = 0
        decorative_skipped = 0
        unknown_dimensions = 0
        duplicate_candidates = 0
        page_errors = 0
        seen_candidates = 0
        visited_pages: set[str] = set()
        queued_pages: list[tuple[int, int, str]] = []
        queued_urls: set[str] = set()
        seen_images: set[str] = set()
        rendered_pages = 0
        declared_collections: dict[str, tuple[str, int]] = {}
        sequence = 0
        max_pages = min(MAX_WEBSITE_PAGES, max(20, limit // 10 + 16))

        def enqueue(url: str, priority_boost: int = 0) -> None:
            nonlocal sequence
            normalized = _canonical_page_url(url)
            if not normalized or normalized in visited_pages or normalized in queued_urls:
                return
            if len(queued_urls) >= MAX_QUEUED_PAGES:
                return
            parsed = urlparse(normalized)
            if not _same_site_host(parsed.netloc, start_host) or not _is_probable_html_page(normalized):
                return
            if robots and not robots.can_fetch(USER_AGENT, normalized):
                _emit(callback, "source_blocked", source=self.source_name, message=f"robots.txt blocked {normalized}")
                return
            sequence += 1
            queued_urls.add(normalized)
            priority = _website_page_priority(normalized) + priority_boost
            heapq.heappush(queued_pages, (-priority, sequence, normalized))

        robots, sitemap_urls = self._load_site_policy(start_url, callback, cancel_event)
        enqueue(start_url, priority_boost=1000)
        for sitemap_page in self._load_sitemap_pages(sitemap_urls, start_host, callback, cancel_event):
            enqueue(sitemap_page, priority_boost=15)

        while queued_pages and len(records) < limit and len(visited_pages) < max_pages:
            _check_cancel(cancel_event)
            _priority, _sequence, page_url = heapq.heappop(queued_pages)
            queued_urls.discard(page_url)
            if page_url in visited_pages:
                continue
            visited_pages.add(page_url)
            _emit(
                callback,
                "source_page",
                source=self.source_name,
                message=f"Scanning page {len(visited_pages)}/{max_pages}: {page_url}",
                current=len(visited_pages),
                total=max_pages,
                queued=len(queued_pages),
                accepted=len(records),
                seen=seen_candidates,
                skipped=skipped_low_resolution,
                low_resolution=skipped_low_resolution,
                irrelevant=decorative_skipped,
                unknown=unknown_dimensions,
                duplicates=duplicate_candidates,
                page_errors=page_errors,
                series=len(declared_collections),
                expected=sum(count for _title, count in declared_collections.values()),
                target=limit,
            )
            try:
                text = self._fetch_html(page_url, start_host)
            except Exception as exc:
                page_errors += 1
                _emit(callback, "source_error", source=self.source_name, message=f"{page_url}: {exc}")
                continue

            discovered_collections = _declared_project_collections(text)
            changed = False
            for collection_id, payload in discovered_collections.items():
                current = declared_collections.get(collection_id)
                if current is None or payload[1] > current[1]:
                    declared_collections[collection_id] = payload
                    changed = True
            if changed:
                declared_total = sum(count for _title, count in declared_collections.values())
                _emit(
                    callback,
                    "source_collection",
                    source=self.source_name,
                    message=f"Discovered {len(declared_collections)} public work series with about {declared_total} declared images.",
                    series=len(declared_collections),
                    expected=declared_total,
                    accepted=len(records),
                    pages=len(visited_pages),
                )

            parser = _WebsiteImageParser(page_url)
            parser.feed(text)
            static_candidates = _best_website_candidates(parser.candidates)
            expected_count = _expected_dynamic_image_count(text)
            should_render = (
                not self._has_usable_candidates(parser, search_name)
                or expected_count > len(static_candidates)
            )
            if (
                rendered_pages < MAX_RENDERED_PAGES
                and self.renderer.available
                and should_render
            ):
                rendered_pages += 1
                if expected_count:
                    _emit(
                        callback,
                        "source_notice",
                        message=f"Page declares {expected_count} works; expanding {len(static_candidates)} static previews.",
                    )
                rendered_text = self.renderer.render(page_url, callback=callback, cancel_event=cancel_event)
                if rendered_text:
                    rendered_parser = _WebsiteImageParser(page_url)
                    rendered_parser.feed(rendered_text)
                    if len(rendered_parser.candidates) >= len(parser.candidates):
                        parser = rendered_parser
            for link in parser.links:
                enqueue(link)

            candidates = _best_website_candidates(parser.candidates)
            duplicate_candidates += max(0, len(parser.candidates) - len(candidates))
            for candidate in candidates:
                _check_cancel(cancel_event)
                normalized_image_url = _strip_tracking_fragment(candidate.image_url)
                image_identity = _website_image_identity(normalized_image_url)
                if not normalized_image_url or not image_identity:
                    continue
                if image_identity in seen_images:
                    duplicate_candidates += 1
                    continue
                seen_candidates += 1
                candidate.collection_title = candidate.collection_title or _collection_title_from_page(
                    parser.page_title,
                    search_name,
                    candidate.page_url or page_url,
                )
                page_title_only = bool(
                    candidate.collection_title
                    and parser.page_title
                    and " ".join(candidate.title.split()).casefold() == " ".join(parser.page_title.split()).casefold()
                )
                if page_title_only or _looks_like_machine_image_title(candidate.title, candidate.image_url):
                    candidate.title = _first_non_empty(
                        candidate.alt,
                        f"{candidate.collection_title} · {seen_candidates:03d}" if candidate.collection_title else "",
                        parser.page_title,
                    )
                record = self._record_from_candidate(candidate, search_name, seen_candidates)
                if _is_decorative_website_image(record, search_name):
                    decorative_skipped += 1
                    _emit(
                        callback,
                        "record_skipped",
                        record=asdict(record),
                        title=record.title,
                        resolution=record.resolution,
                        reason="decorative site image",
                        accepted=len(records),
                        seen=seen_candidates,
                        skipped=skipped_low_resolution + decorative_skipped,
                        low_resolution=skipped_low_resolution,
                        irrelevant=decorative_skipped,
                        unknown=unknown_dimensions,
                        duplicates=duplicate_candidates,
                        page_errors=page_errors,
                        target=limit,
                    )
                    continue
                if min_long_edge and record.long_edge and record.long_edge < min_long_edge:
                    skipped_low_resolution += 1
                    _emit(
                        callback,
                        "record_skipped",
                        record=asdict(record),
                        title=record.title,
                        resolution=record.resolution,
                        reason=f"long edge {record.long_edge}px below {min_long_edge}px",
                        accepted=len(records),
                        seen=seen_candidates,
                        skipped=skipped_low_resolution + decorative_skipped,
                        low_resolution=skipped_low_resolution,
                        irrelevant=decorative_skipped,
                        unknown=unknown_dimensions,
                        duplicates=duplicate_candidates,
                        page_errors=page_errors,
                        target=limit,
                    )
                    continue
                if not record.long_edge:
                    unknown_dimensions += 1
                seen_images.add(image_identity)
                records.append(record)
                _emit(
                    callback,
                    "record_found",
                    record=asdict(record),
                    title=record.title,
                    resolution=record.resolution,
                    accepted=len(records),
                    seen=seen_candidates,
                    skipped=skipped_low_resolution + decorative_skipped,
                    low_resolution=skipped_low_resolution,
                    irrelevant=decorative_skipped,
                    unknown=unknown_dimensions,
                    duplicates=duplicate_candidates,
                    page_errors=page_errors,
                    target=limit,
                )
                if len(records) >= limit:
                    break

        declared_total = sum(count for _title, count in declared_collections.values())
        _emit(
            callback,
            "source_complete",
            source=self.source_name,
            message=f"Website scan completed after {len(visited_pages)} pages; accepted {len(records)} records.",
            pages=len(visited_pages),
            accepted=len(records),
            series=len(declared_collections),
            expected=declared_total,
            page_errors=page_errors,
            limit_reached=len(records) >= limit,
        )
        return SearchResult(
            records=records,
            found=seen_candidates,
            skipped_low_resolution=skipped_low_resolution,
            rejected_irrelevant=decorative_skipped,
            unknown_dimensions=unknown_dimensions,
            duplicate_candidates=duplicate_candidates,
            page_errors=page_errors,
            pages_scanned=len(visited_pages),
            series_count=len(declared_collections),
            declared_total=declared_total,
        )

    def _has_usable_candidates(self, parser: "_WebsiteImageParser", search_name: str) -> bool:
        for index, candidate in enumerate(_best_website_candidates(parser.candidates), start=1):
            record = self._record_from_candidate(candidate, search_name, index)
            if not _is_decorative_website_image(record, search_name):
                return True
        return False

    def _load_site_policy(
        self,
        start_url: str,
        callback: Optional[ProgressCallback],
        cancel_event: Optional[Event],
    ) -> tuple[RobotFileParser, list[str]]:
        parsed = urlparse(start_url)
        robots_url = urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
        parser = RobotFileParser(robots_url)
        sitemap_urls: list[str] = []
        _emit(callback, "source_index", message=f"Reading site policy: {robots_url}")
        _check_cancel(cancel_event)
        try:
            response, content = self._bounded_get(
                robots_url,
                timeout=(4, 7),
                max_bytes=MAX_ROBOTS_BYTES,
                expected_host=parsed.netloc.lower(),
                label="robots.txt",
            )
            lines = content.decode(getattr(response, "encoding", None) or "utf-8", errors="replace").splitlines()
            parser.parse(lines)
            for line in lines:
                key, separator, value = line.partition(":")
                if separator and key.strip().casefold() == "sitemap" and value.strip():
                    sitemap_urls.append(urljoin(start_url, value.strip()))
        except Exception as exc:
            parser.parse([])
            _emit(callback, "source_notice", message=f"Site policy unavailable; continuing with public pages: {exc}")
        return parser, sitemap_urls[:4]

    def _load_sitemap_pages(
        self,
        sitemap_urls: list[str],
        start_host: str,
        callback: Optional[ProgressCallback],
        cancel_event: Optional[Event],
    ) -> list[str]:
        pages: list[str] = []
        pending = list(sitemap_urls)
        visited: set[str] = set()
        while pending and len(visited) < 8 and len(pages) < MAX_SITEMAP_URLS:
            _check_cancel(cancel_event)
            sitemap_url = pending.pop(0)
            if sitemap_url in visited or not _same_site_host(urlparse(sitemap_url).netloc, start_host):
                continue
            visited.add(sitemap_url)
            _emit(callback, "source_index", message=f"Reading sitemap: {sitemap_url}")
            try:
                _response, content = self._bounded_get(
                    sitemap_url,
                    timeout=(5, 10),
                    max_bytes=MAX_SITEMAP_BYTES,
                    expected_host=start_host,
                    label="sitemap",
                )
                root = ET.fromstring(content)
            except Exception as exc:
                _emit(callback, "source_notice", message=f"Skipped sitemap {sitemap_url}: {exc}")
                continue
            locations = [str(node.text or "").strip() for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "loc"]
            if root.tag.rsplit("}", 1)[-1] == "sitemapindex":
                pending.extend(locations[:8])
            else:
                pages.extend(locations[: MAX_SITEMAP_URLS - len(pages)])
        if pages:
            _emit(callback, "source_index", message=f"Sitemap supplied {len(pages)} candidate pages")
        return pages

    def _bounded_get(
        self,
        url: str,
        *,
        timeout: tuple[int, int],
        max_bytes: int,
        expected_host: str,
        label: str,
    ):
        current_url = url
        for redirect_count in range(MAX_SAME_SITE_REDIRECTS + 1):
            if not _same_site_host(urlparse(current_url).netloc, expected_host):
                raise PhotoArchiveError(f"{label} redirected outside the official site: {current_url}")
            response = self.session.get(
                current_url,
                timeout=timeout,
                stream=True,
                allow_redirects=False,
            )
            try:
                status_code = int(getattr(response, "status_code", 200) or 200)
                if status_code in {301, 302, 303, 307, 308}:
                    location = str(response.headers.get("Location", "")).strip()
                    if not location:
                        raise PhotoArchiveError(f"{label} returned a redirect without a location")
                    if redirect_count >= MAX_SAME_SITE_REDIRECTS:
                        raise PhotoArchiveError(f"{label} exceeded the redirect limit")
                    current_url = urljoin(current_url, location)
                    continue
                response.raise_for_status()
                final_url = str(getattr(response, "url", "") or current_url)
                if not _same_site_host(urlparse(final_url).netloc, expected_host):
                    raise PhotoArchiveError(f"{label} redirected outside the official site: {final_url}")
                content = _bounded_response_content(response, max_bytes, label)
                return response, content
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        raise PhotoArchiveError(f"{label} exceeded the redirect limit")

    def _fetch_html(self, url: str, start_host: str = "") -> str:
        start_host = start_host or urlparse(url).netloc.lower()
        response, content = self._bounded_get(
            url,
            timeout=(8, 25),
            max_bytes=MAX_WEBSITE_HTML_BYTES,
            expected_host=start_host,
            label="HTML page",
        )
        content_type = response.headers.get("Content-Type", "").lower()
        if content_type and "html" not in content_type:
            raise PhotoArchiveError(f"not an HTML page: {content_type}")
        encoding = response.encoding or "utf-8"
        if "charset=" not in content_type and encoding.casefold() in {"iso-8859-1", "latin-1"}:
            encoding = "utf-8"
        return content.decode(encoding, errors="replace")

    def _record_from_candidate(self, candidate: WebsiteImageCandidate, search_name: str, index: int) -> PhotoRecord:
        source_id = hashlib.sha1(candidate.image_url.encode("utf-8")).hexdigest()[:20]
        title = _first_non_empty(candidate.title, candidate.alt, Path(urlparse(candidate.image_url).path).stem, f"website-image-{index}")
        return PhotoRecord(
            source=self.source_name,
            source_id=source_id,
            search_name=search_name,
            title=title,
            collection_title=candidate.collection_title,
            page_url=candidate.page_url,
            image_url=candidate.image_url,
            thumb_url=candidate.thumb_url or candidate.image_url,
            match_confidence=100,
            match_reason="official website",
            width=candidate.width,
            height=candidate.height,
            mime=mimetypes.guess_type(urlparse(candidate.image_url).path)[0] or "",
            author=search_name,
            license_name="Website preview",
            annotation=candidate.alt,
            source_comment=(
                f"Official website collection: {candidate.collection_title}. "
                "Open the source page or original image URL for review."
                if candidate.collection_title
                else "Found on photographer website. Open the source page or original image URL for review."
            ),
        )


class _WebsiteImageParser(HTMLParser):
    def __init__(
        self,
        base_url: str,
        *,
        max_links: int = MAX_PAGE_LINKS,
        max_candidates: int = MAX_PAGE_CANDIDATES,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.max_links = max(0, int(max_links))
        self.max_candidates = max(0, int(max_candidates))
        self.candidates: list[WebsiteImageCandidate] = []
        self.links: list[str] = []
        self._current_link = base_url
        self._link_stack: list[str] = []
        self._in_title = False
        self._title_parts: list[str] = []
        self._last_meta_candidate: Optional[WebsiteImageCandidate] = None
        self._figure_stack: list[dict[str, object]] = []

    @property
    def page_title(self) -> str:
        return html_to_text(" ".join(self._title_parts))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        data = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
            return
        if tag == "figure":
            self._figure_stack.append({"start": len(self.candidates), "caption_depth": 0, "caption": []})
        elif tag == "figcaption" and self._figure_stack:
            self._figure_stack[-1]["caption_depth"] = int(self._figure_stack[-1]["caption_depth"]) + 1
        if tag == "a":
            href = data.get("href", "").strip()
            self._link_stack.append(self._current_link)
            if href:
                self._current_link = urljoin(self.base_url, href)
                if len(self.links) < self.max_links:
                    self.links.append(self._current_link)
            return
        if tag == "meta":
            self._handle_meta(data)
            return
        if tag not in {"img", "source"}:
            style_url = _style_image_url(data.get("style", ""))
            if style_url:
                self._append_candidate(style_url, style_url, data)
            return

        raw_src = _first_non_empty(
            data.get("data-image"),
            data.get("data-original"),
            data.get("data-lazy-src"),
            data.get("data-src"),
            _best_srcset_url(data.get("data-srcset", "")),
            _best_srcset_url(data.get("data-lazy-srcset", "")),
            _best_srcset_url(data.get("srcset", "")),
            data.get("src"),
        )
        if not raw_src:
            return
        thumb_src = _first_non_empty(data.get("src"), data.get("data-lazy-src"), data.get("data-src"), raw_src)
        self._append_candidate(raw_src, thumb_src, data)

    def _append_candidate(self, raw_src: str, thumb_src: str, data: dict[str, str]) -> None:
        if len(self.candidates) >= self.max_candidates:
            return
        parsed_source = urljoin(self.base_url, raw_src)
        thumb_url = urljoin(self.base_url, thumb_src)
        linked_image = self._current_link if _is_direct_image_url(self._current_link) else ""
        image_url = linked_image or parsed_source
        page_url = self.base_url if linked_image else (self._current_link or self.base_url)
        width, height, dimensions_verified = _intrinsic_dimensions_from_attrs(data)
        if linked_image and _website_image_identity(linked_image) != _website_image_identity(parsed_source):
            width, height, dimensions_verified = 0, 0, False
        inferred_width, inferred_height = _image_dimensions_from_url(image_url)
        if inferred_width and inferred_height:
            width, height, dimensions_verified = inferred_width, inferred_height, True
        alt = html_to_text(
            _first_non_empty(
                data.get("alt"),
                data.get("data-caption"),
                data.get("data-title"),
                data.get("aria-label"),
            )
        )
        title = _first_non_empty(
            data.get("data-caption"),
            data.get("data-title"),
            data.get("aria-label"),
            data.get("title"),
            alt,
            Path(urlparse(image_url).path).stem,
        )
        self.candidates.append(
            WebsiteImageCandidate(
                image_url=image_url,
                thumb_url=thumb_url,
                page_url=page_url,
                title=title,
                alt=alt,
                width=width,
                height=height,
                dimensions_verified=dimensions_verified,
            )
        )

    def _handle_meta(self, data: dict[str, str]) -> None:
        key = _first_non_empty(data.get("property"), data.get("name")).casefold()
        content = data.get("content", "").strip()
        if key in {"og:image", "og:image:url", "twitter:image", "twitter:image:src"} and content:
            if len(self.candidates) >= self.max_candidates:
                self._last_meta_candidate = None
                return
            candidate = WebsiteImageCandidate(
                image_url=urljoin(self.base_url, content),
                thumb_url=urljoin(self.base_url, content),
                page_url=self.base_url,
                title=self.page_title,
            )
            self.candidates.append(candidate)
            self._last_meta_candidate = candidate
        elif self._last_meta_candidate and key in {"og:image:width", "twitter:image:width"}:
            self._last_meta_candidate.width = _safe_int(content)
            self._last_meta_candidate.dimensions_verified = bool(self._last_meta_candidate.width and self._last_meta_candidate.height)
        elif self._last_meta_candidate and key in {"og:image:height", "twitter:image:height"}:
            self._last_meta_candidate.height = _safe_int(content)
            self._last_meta_candidate.dimensions_verified = bool(self._last_meta_candidate.width and self._last_meta_candidate.height)

    def handle_data(self, data: str) -> None:
        if self._in_title and data.strip():
            self._title_parts.append(data.strip())
        if self._figure_stack and int(self._figure_stack[-1]["caption_depth"]) > 0 and data.strip():
            caption = self._figure_stack[-1]["caption"]
            if isinstance(caption, list):
                caption.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        elif tag == "figcaption" and self._figure_stack:
            self._figure_stack[-1]["caption_depth"] = max(
                0,
                int(self._figure_stack[-1]["caption_depth"]) - 1,
            )
        elif tag == "figure" and self._figure_stack:
            frame = self._figure_stack.pop()
            caption_parts = frame.get("caption") or []
            caption = html_to_text(" ".join(str(part) for part in caption_parts))
            start = int(frame.get("start") or 0)
            if caption:
                for candidate in self.candidates[start:]:
                    if not candidate.alt or _looks_like_machine_image_title(candidate.alt, candidate.image_url):
                        candidate.alt = caption
                    if _looks_like_machine_image_title(candidate.title, candidate.image_url):
                        candidate.title = caption
        elif tag == "a":
            self._current_link = self._link_stack.pop() if self._link_stack else self.base_url


class WikimediaCommonsSource:
    source_name = "wikimedia_commons"

    def __init__(self, session: Optional[requests.Session] = None) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def search(
        self,
        query: str,
        limit: int = 40,
        min_long_edge: int = DEFAULT_MIN_LONG_EDGE,
        callback: Optional[ProgressCallback] = None,
        cancel_event: Optional[Event] = None,
    ) -> SearchResult:
        """Search Commons and accept only files attributable to the photographer.

        MediaWiki full-text rank is treated as candidate discovery, not proof of
        authorship. Metadata must contain a strong name/credit/category match.
        @codex-comment approved
        """

        records: list[PhotoRecord] = []
        skipped_low_resolution = 0
        rejected_irrelevant = 0
        total_seen = 0
        params: dict[str, object] = {
            "action": "query",
            "format": "json",
            "formatversion": 2,
            "maxlag": 5,
            "generator": "search",
            "gsrnamespace": 6,
            "gsrsearch": _commons_search_query(query),
            "gsrlimit": min(50, max(limit * 2, 10)),
            "prop": "imageinfo|categories",
            "iiprop": "url|size|mime|extmetadata|metadata|commonmetadata",
            "iiurlwidth": 640,
            "cllimit": 20,
        }

        while len(records) < limit:
            _check_cancel(cancel_event)
            try:
                _emit(
                    callback,
                    "source_page",
                    source=self.source_name,
                    message=f"Requesting Wikimedia Commons page; accepted {len(records)}/{limit}, checked {total_seen}.",
                    accepted=len(records),
                    seen=total_seen,
                    skipped=skipped_low_resolution,
                    target=limit,
                )
                data = _get_json_with_retries(
                    self.session,
                    COMMONS_API_URL,
                    params,
                    callback,
                    cancel_event=cancel_event,
                )
            except Exception as exc:
                _emit(callback, "source_error", source=self.source_name, message=str(exc))
                break
            pages = (data.get("query") or {}).get("pages") or []
            if not pages:
                break

            for page in sorted(pages, key=lambda item: item.get("index", 999999)):
                _check_cancel(cancel_event)
                total_seen += 1
                try:
                    record = self._record_from_page(page, query)
                except Exception as exc:
                    _emit(
                        callback,
                        "record_error",
                        title=str(page.get("title") or page.get("pageid") or "unknown"),
                        message=str(exc),
                        seen=total_seen,
                    )
                    _emit(
                        callback,
                        "search_progress",
                        seen=total_seen,
                        accepted=len(records),
                        skipped=skipped_low_resolution,
                        target=limit,
                    )
                    continue
                if record is None:
                    _emit(
                        callback,
                        "search_progress",
                        seen=total_seen,
                        accepted=len(records),
                        skipped=skipped_low_resolution,
                        target=limit,
                    )
                    continue
                match_score, match_reason = _commons_match_score(record, query)
                if match_score < COMMONS_MIN_MATCH_SCORE:
                    rejected_irrelevant += 1
                    _emit(
                        callback,
                        "record_rejected",
                        title=record.title,
                        resolution=record.resolution,
                        reason="no reliable photographer attribution match",
                        match_score=match_score,
                        accepted=len(records),
                        seen=total_seen,
                        skipped=skipped_low_resolution,
                        rejected=rejected_irrelevant,
                        target=limit,
                    )
                    continue
                record.match_confidence = match_score
                record.match_reason = match_reason
                if min_long_edge and record.long_edge < min_long_edge:
                    skipped_low_resolution += 1
                    _emit(
                        callback,
                        "record_skipped",
                        record=asdict(record),
                        title=record.title,
                        resolution=record.resolution,
                        reason=f"long edge {record.long_edge}px below {min_long_edge}px",
                        accepted=len(records),
                        seen=total_seen,
                        skipped=skipped_low_resolution,
                        target=limit,
                    )
                    _emit(
                        callback,
                        "search_progress",
                        seen=total_seen,
                        accepted=len(records),
                        skipped=skipped_low_resolution,
                        target=limit,
                    )
                    continue
                records.append(record)
                _emit(
                    callback,
                    "record_found",
                    record=asdict(record),
                    title=record.title,
                    resolution=record.resolution,
                    accepted=len(records),
                    seen=total_seen,
                    skipped=skipped_low_resolution,
                    target=limit,
                )
                _emit(
                    callback,
                    "search_progress",
                    seen=total_seen,
                    accepted=len(records),
                    skipped=skipped_low_resolution,
                    target=limit,
                )
                if len(records) >= limit:
                    break

            continuation = data.get("continue")
            if not continuation:
                break
            for key, value in continuation.items():
                params[key] = value

        return SearchResult(
            records=records,
            found=total_seen,
            skipped_low_resolution=skipped_low_resolution,
            rejected_irrelevant=rejected_irrelevant,
        )

    def _record_from_page(self, page: dict, query: str) -> Optional[PhotoRecord]:
        image_info_list = page.get("imageinfo") or []
        if not image_info_list:
            return None
        info = image_info_list[0]
        mime = str(info.get("mime") or "")
        if mime and not mime.startswith("image/"):
            return None

        ext = _extract_extmetadata(info.get("extmetadata") or {})
        metadata = _metadata_list_to_map(info.get("metadata") or [])
        commonmetadata = _metadata_list_to_map(info.get("commonmetadata") or [])
        merged_metadata = {**commonmetadata, **metadata}

        page_title = str(page.get("title") or "")
        clean_title = _file_title_to_name(page_title)
        title = _first_non_empty(ext.get("ObjectName"), ext.get("Headline"), clean_title)
        annotation = _first_non_empty(
            ext.get("ImageDescription"),
            merged_metadata.get("ImageDescription"),
            merged_metadata.get("Caption-Abstract"),
            merged_metadata.get("XPTitle"),
        )
        source_comment = _join_non_empty(
            ext.get("CreditLine"),
            ext.get("Permission"),
            ext.get("UsageTerms"),
            merged_metadata.get("UserComment"),
            merged_metadata.get("XPComment"),
        )
        author = _first_non_empty(ext.get("Artist"), merged_metadata.get("Artist"), ext.get("Attribution"))

        raw_metadata = {
            "commons_title": page_title,
            "categories": [item.get("title", "") for item in page.get("categories") or []],
            "extmetadata": ext,
            "metadata": metadata,
            "commonmetadata": commonmetadata,
        }

        return PhotoRecord(
            source=self.source_name,
            source_id=str(page.get("pageid") or page_title),
            search_name=query,
            title=title,
            page_url=str(info.get("descriptionurl") or f"https://commons.wikimedia.org/wiki/{page_title.replace(' ', '_')}"),
            image_url=str(info.get("url") or ""),
            thumb_url=str(info.get("thumburl") or ""),
            width=_safe_int(info.get("width")),
            height=_safe_int(info.get("height")),
            mime=mime,
            file_size=_safe_int(info.get("size")),
            author=author,
            license_name=_first_non_empty(ext.get("LicenseShortName"), ext.get("License"), ext.get("UsageTerms")),
            license_url=ext.get("LicenseUrl", ""),
            credit=ext.get("CreditLine", ""),
            annotation=annotation,
            source_comment=source_comment,
            shooting_date=_first_non_empty(
                ext.get("DateTimeOriginal"),
                merged_metadata.get("DateTimeOriginal"),
                merged_metadata.get("DateTime"),
                ext.get("DateTime"),
            ),
            camera_make=_first_non_empty(merged_metadata.get("Make"), merged_metadata.get("CameraManufacturer")),
            camera_model=_first_non_empty(merged_metadata.get("Model"), merged_metadata.get("CameraModel")),
            lens_model=_first_non_empty(merged_metadata.get("LensModel"), merged_metadata.get("Lens")),
            exposure_time=_first_non_empty(merged_metadata.get("ExposureTime"), merged_metadata.get("Exposure")),
            f_number=_first_non_empty(merged_metadata.get("FNumber"), merged_metadata.get("ApertureValue")),
            iso=_first_non_empty(merged_metadata.get("ISOSpeedRatings"), merged_metadata.get("ISO")),
            focal_length=_first_non_empty(merged_metadata.get("FocalLength"), merged_metadata.get("FocalLengthIn35mmFilm")),
            raw_metadata_json=json.dumps(raw_metadata, ensure_ascii=False, sort_keys=True),
        )


class ArchiveStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def connection(self):
        conn = self.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    search_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    collection_title TEXT DEFAULT '',
                    page_url TEXT NOT NULL,
                    image_url TEXT NOT NULL,
                    thumb_url TEXT DEFAULT '',
                    match_confidence INTEGER DEFAULT 0,
                    match_reason TEXT DEFAULT '',
                    width INTEGER DEFAULT 0,
                    height INTEGER DEFAULT 0,
                    mime TEXT DEFAULT '',
                    file_size INTEGER DEFAULT 0,
                    author TEXT DEFAULT '',
                    license_name TEXT DEFAULT '',
                    license_url TEXT DEFAULT '',
                    credit TEXT DEFAULT '',
                    annotation TEXT DEFAULT '',
                    source_comment TEXT DEFAULT '',
                    research_note TEXT DEFAULT '',
                    research_tags TEXT DEFAULT '',
                    rating INTEGER DEFAULT 0,
                    shooting_date TEXT DEFAULT '',
                    camera_make TEXT DEFAULT '',
                    camera_model TEXT DEFAULT '',
                    lens_model TEXT DEFAULT '',
                    exposure_time TEXT DEFAULT '',
                    f_number TEXT DEFAULT '',
                    iso TEXT DEFAULT '',
                    focal_length TEXT DEFAULT '',
                    raw_metadata_json TEXT DEFAULT '{}',
                    local_path TEXT DEFAULT '',
                    sha256 TEXT DEFAULT '',
                    dhash TEXT DEFAULT '',
                    duplicate_of TEXT DEFAULT '',
                    near_duplicate_of TEXT DEFAULT '',
                    duplicate_distance INTEGER DEFAULT -1,
                    downloaded_at TEXT DEFAULT '',
                    UNIQUE(source, source_id)
                )
                """
            )
            existing_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(photos)").fetchall()}
            for column, declaration in {
                "match_confidence": "INTEGER DEFAULT 0",
                "match_reason": "TEXT DEFAULT ''",
                "collection_title": "TEXT DEFAULT ''",
                "research_note": "TEXT DEFAULT ''",
                "research_tags": "TEXT DEFAULT ''",
                "rating": "INTEGER DEFAULT 0",
            }.items():
                if column not in existing_columns:
                    conn.execute(f"ALTER TABLE photos ADD COLUMN {column} {declaration}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_search_name ON photos(search_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_sha256 ON photos(sha256)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_dhash ON photos(dhash)")

    def upsert(self, record: PhotoRecord) -> PhotoRecord:
        existing = self.get_by_source(record.source, record.source_id)
        if existing:
            record = _merge_record(existing, record)
        record.research_tags = normalize_research_tags(record.research_tags)
        record.rating = max(0, min(5, _safe_int(record.rating)))

        placeholders = ", ".join("?" for _ in PHOTO_COLUMNS)
        user_columns = {"research_note", "research_tags", "rating"}
        update_columns = ", ".join(
            f"{column}=photos.{column}" if column in user_columns else f"{column}=excluded.{column}"
            for column in PHOTO_COLUMNS
            if column not in {"source", "source_id"}
        )
        values = [_record_value(record, column) for column in PHOTO_COLUMNS]
        with self.connection() as conn:
            conn.execute(
                f"""
                INSERT INTO photos ({", ".join(PHOTO_COLUMNS)})
                VALUES ({placeholders})
                ON CONFLICT(source, source_id) DO UPDATE SET {update_columns}
                """,
                values,
            )
        return self.get_by_source(record.source, record.source_id) or record

    def get_by_source(self, source: str, source_id: str) -> Optional[PhotoRecord]:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM photos WHERE source = ? AND source_id = ?",
                (source, source_id),
            ).fetchone()
        return _row_to_record(row) if row else None

    def get_by_sha256(self, sha256: str, exclude_source_key: str = "") -> Optional[PhotoRecord]:
        if not sha256:
            return None
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM photos WHERE sha256 = ? AND local_path != '' ORDER BY downloaded_at ASC, id ASC",
                (sha256,),
            ).fetchall()
        for row in rows:
            record = _row_to_record(row)
            if record and record.source_key != exclude_source_key:
                return record
        return None

    def list_records(self, search_name: str = "") -> list[PhotoRecord]:
        with self.connection() as conn:
            if search_name:
                rows = conn.execute(
                    "SELECT * FROM photos WHERE search_name = ? ORDER BY downloaded_at DESC, title ASC",
                    (search_name,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM photos ORDER BY search_name ASC, title ASC").fetchall()
        return [record for row in rows if (record := _row_to_record(row))]

    def update_research_content(
        self,
        source: str,
        source_id: str,
        research_note: str,
        research_tags: str,
        rating: int,
    ) -> Optional[PhotoRecord]:
        normalized_tags = normalize_research_tags(research_tags)
        normalized_rating = max(0, min(5, _safe_int(rating)))
        with self.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE photos
                SET research_note = ?, research_tags = ?, rating = ?
                WHERE source = ? AND source_id = ?
                """,
                (research_note.strip(), normalized_tags, normalized_rating, source, source_id),
            )
        if not cursor.rowcount:
            return None
        return self.get_by_source(source, source_id)

    def list_search_names(self) -> list[tuple[str, int]]:
        """Return saved photographer queries ordered by recent archive activity.

        The count reflects metadata records, including items that are not downloaded.
        @codex-comment approved
        """

        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT search_name, COUNT(*) AS record_count, MAX(id) AS latest_id
                FROM photos
                WHERE search_name != ''
                GROUP BY search_name
                ORDER BY latest_id DESC, search_name COLLATE NOCASE ASC
                """
            ).fetchall()
        return [(str(row["search_name"]), int(row["record_count"] or 0)) for row in rows]

    def delete_search(self, search_name: str) -> int:
        if not search_name.strip():
            return 0
        with self.connection() as conn:
            cursor = conn.execute("DELETE FROM photos WHERE search_name = ?", (search_name.strip(),))
            return int(cursor.rowcount or 0)

    def downloaded_records(self, exclude_source_key: str = "") -> list[PhotoRecord]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM photos WHERE local_path != '' AND dhash != '' ORDER BY downloaded_at ASC, id ASC"
            ).fetchall()
        records = [record for row in rows if (record := _row_to_record(row))]
        return [record for record in records if record.source_key != exclude_source_key]


def download_record(
    record: PhotoRecord,
    output_root: Path | str,
    store: ArchiveStore,
    near_duplicate_distance: int = DEFAULT_NEAR_DUPLICATE_DISTANCE,
    callback: Optional[ProgressCallback] = None,
    cancel_event: Optional[Event] = None,
) -> PhotoRecord:
    if not record.image_url:
        raise PhotoArchiveError(f"No downloadable URL for {record.title}")

    output_root = Path(output_root).expanduser().resolve()
    image_dir = output_root / safe_filename(record.search_name, "photographer") / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    destination = _destination_for_record(record, image_dir)
    part_path = destination.with_suffix(destination.suffix + ".part")

    if not destination.exists():
        try:
            _download_binary(
                record.image_url,
                part_path,
                callback=callback,
                title=record.title,
                cancel_event=cancel_event,
            )
            part_path.replace(destination)
        except Exception:
            part_path.unlink(missing_ok=True)
            raise

    sha256 = sha256_file(destination)
    dhash = dhash_file(destination)
    exif_details = extract_exif_details(destination)
    if Image is not None:
        try:
            with Image.open(destination) as downloaded_image:
                record.width, record.height = downloaded_image.size
        except (OSError, ValueError):
            pass
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    record.local_path = str(destination)
    record.sha256 = sha256
    record.dhash = dhash
    record.downloaded_at = now
    for field, value in exif_details.items():
        if value and not getattr(record, field):
            setattr(record, field, value)
    record.duplicate_of = ""
    record.near_duplicate_of = ""
    record.duplicate_distance = -1

    exact = store.get_by_sha256(sha256, exclude_source_key=record.source_key)
    if exact:
        record.duplicate_of = exact.source_key
        record.local_path = exact.local_path
        try:
            if destination.exists() and Path(exact.local_path).resolve() != destination.resolve():
                destination.unlink()
        except OSError:
            pass
        _emit(callback, "duplicate", title=record.title, duplicate_of=exact.title, kind="exact")
        return store.upsert(record)

    near = find_near_duplicate(record, store.downloaded_records(exclude_source_key=record.source_key), near_duplicate_distance)
    if near:
        duplicate, distance = near
        record.near_duplicate_of = duplicate.source_key
        record.duplicate_distance = distance
        _emit(
            callback,
            "duplicate",
            title=record.title,
            duplicate_of=duplicate.title,
            kind="near",
            distance=distance,
        )

    _emit(callback, "downloaded", title=record.title, path=record.local_path)
    return store.upsert(record)


def find_near_duplicate(
    record: PhotoRecord,
    candidates: Iterable[PhotoRecord],
    max_distance: int = DEFAULT_NEAR_DUPLICATE_DISTANCE,
) -> Optional[tuple[PhotoRecord, int]]:
    if not record.dhash:
        return None
    best: Optional[tuple[PhotoRecord, int]] = None
    for candidate in candidates:
        if not candidate.dhash:
            continue
        distance = hamming_distance_hex(record.dhash, candidate.dhash)
        if distance <= max_distance and (best is None or distance < best[1]):
            best = (candidate, distance)
    return best


def dhash_file(path: Path) -> str:
    if Image is None:
        return ""
    try:
        with Image.open(path) as img:
            img = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
            pixels = list(img.getdata())
    except Exception:
        return ""

    bits = 0
    for row in range(8):
        for col in range(8):
            left = pixels[row * 9 + col]
            right = pixels[row * 9 + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return f"{bits:016x}"


def extract_exif_details(path: Path) -> dict[str, str]:
    """Read non-location shooting details from a local image when EXIF survives.

    GPS and free-form private tags are intentionally excluded. Values are normalized
    for direct display and never overwrite richer source metadata. @codex-comment approved
    """

    if Image is None or ExifTags is None:
        return {}
    try:
        with Image.open(path) as image:
            raw_exif = image.getexif()
            values = {str(ExifTags.TAGS.get(tag, tag)): value for tag, value in raw_exif.items()}
    except Exception:
        return {}
    return {
        "shooting_date": _format_exif_value(values.get("DateTimeOriginal") or values.get("DateTimeDigitized") or values.get("DateTime")),
        "camera_make": _format_exif_value(values.get("Make")),
        "camera_model": _format_exif_value(values.get("Model")),
        "lens_model": _format_exif_value(values.get("LensModel") or values.get("LensMake")),
        "exposure_time": _format_exif_value(values.get("ExposureTime")),
        "f_number": _format_exif_value(values.get("FNumber")),
        "iso": _format_exif_value(values.get("ISOSpeedRatings") or values.get("PhotographicSensitivity")),
        "focal_length": _format_exif_value(values.get("FocalLength")),
    }


def _format_exif_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip("\x00 ")
    return str(value).strip()


def hamming_distance_hex(left: str, right: str) -> int:
    if not left or not right:
        return 999
    return (int(left, 16) ^ int(right, 16)).bit_count()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_filename(value: str, default: str = "file", max_length: int = 120) -> str:
    value = html.unescape(unquote(value or "")).replace("\xa0", " ").strip()
    value = re.sub(r"[\\/:*?\"<>|\r\n\t]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = default
    if len(value) > max_length:
        value = value[:max_length].rstrip(" .")
    return value or default


def html_to_text(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def _normalize_person_name(value: object) -> str:
    return re.sub(r"[\W_]+", "", html_to_text(value).casefold(), flags=re.UNICODE)


def _find_chromium_executable() -> Optional[Path]:
    for command in ("msedge", "chrome", "chromium", "chromium-browser"):
        resolved = shutil.which(command)
        if resolved:
            return Path(resolved)
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    return next((path for path in candidates if path.is_file()), None)


def _commons_search_query(query: str) -> str:
    cleaned = query.strip().replace('"', " ")
    return f'"{cleaned}"' if " " in cleaned else cleaned


def _commons_match_score(record: PhotoRecord, query: str) -> tuple[int, str]:
    name = _normalize_person_name(query)
    if not name:
        return 0, "empty photographer name"
    for score, reason, value in (
        (100, "author metadata", record.author),
        (96, "credit metadata", record.credit),
        (88, "source credit", record.source_comment),
    ):
        if name in _normalize_person_name(value):
            return score, reason

    annotation = html_to_text(record.annotation).casefold()
    normalized_annotation = _normalize_person_name(annotation)
    byline_tokens = ("photo by", "photograph by", "photography by", "fotograf", "摄影", "撮影")
    if name in normalized_annotation and any(token in annotation for token in byline_tokens):
        return 90, "caption byline"

    try:
        raw = json.loads(record.raw_metadata_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        raw = {}
    categories = " ".join(str(item) for item in raw.get("categories", []) if item)
    normalized_categories = _normalize_person_name(categories)
    category_text = categories.casefold()
    if name in normalized_categories and any(token in category_text for token in ("photographs by", "photos by", "摄影作品", "撮影")):
        return 92, "photographer category"
    return 0, "name appears only in general search context"


def _normalize_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise PhotoArchiveError("Website URL is empty.")
    parsed = urlparse(value)
    if not parsed.scheme:
        value = "https://" + value
    return value


def _canonical_page_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", parsed.query, ""))


def _same_site_host(left: str, right: str) -> bool:
    def normalized(value: str) -> str:
        host = value.casefold().rsplit("@", 1)[-1].split(":", 1)[0].strip(".")
        return host.removeprefix("www.")

    return bool(normalized(left)) and normalized(left) == normalized(right)


def _collection_title_from_page(page_title: str, search_name: str, page_url: str) -> str:
    title = " ".join(html_to_text(page_title).split())
    normalized_name = _normalize_person_name(search_name)
    generic = {
        "home",
        "homepage",
        "officialsite",
        "officialwebsite",
        "photography",
        "photographer",
        "portfolio",
        "works",
    }
    if title:
        parts = [part.strip(" -|·—–") for part in re.split(r"\s+(?:[-|·—–])\s+|[|·]", title) if part.strip()]
        for part in parts:
            normalized = _normalize_person_name(part)
            if not normalized or normalized in generic:
                continue
            if normalized_name and (normalized == normalized_name or normalized_name in normalized):
                continue
            return part

    path_parts = [unquote(part) for part in urlparse(page_url).path.split("/") if part]
    if path_parts:
        fallback = " ".join(path_parts[-1].replace("_", " ").replace("-", " ").split())
        normalized = _normalize_person_name(fallback)
        if normalized and normalized not in generic:
            return fallback
    return ""


def _looks_like_machine_image_title(title: str, image_url: str = "") -> bool:
    value = " ".join(html_to_text(title).split()).strip()
    if not value:
        return True
    stem = unquote(Path(urlparse(image_url).path).stem).strip()
    comparable = re.sub(r"\s+", "", value).casefold()
    machine_patterns = (
        r"^[a-f0-9]{20,}(?:-\d{2,5}x\d{2,5})?$",
        r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$",
        r"^(?:img|dsc|image|photo|picture)[-_]?\d{3,}$",
        r"^\d{6,}$",
    )
    if any(re.fullmatch(pattern, comparable, flags=re.IGNORECASE) for pattern in machine_patterns):
        return True
    if stem and value.casefold() == stem.casefold():
        compact_stem = re.sub(r"[-_]", "", stem)
        return any(re.fullmatch(pattern, compact_stem, flags=re.IGNORECASE) for pattern in machine_patterns)
    return False


def _website_page_priority(url: str) -> int:
    parsed = urlparse(url)
    text = f"{parsed.path} {parsed.query}".casefold()
    score = 0
    for token in (
        "portfolio",
        "project",
        "projects",
        "bodies-of-work",
        "work",
        "works",
        "series",
        "story",
        "stories",
        "gallery",
        "archive",
        "photograph",
        "countries",
        "celebrities",
        "politicians",
        "作品",
        "系列",
        "项目",
    ):
        if token in text:
            score += 35
    for token in ("book", "books", "writing", "writings", "video", "videos", "press", "interview", "texts", "news", "event", "exhibition"):
        if token in text:
            score -= 70
    for token in ("about", "contact", "shop", "store", "cart", "privacy", "terms", "bio", "login", "account"):
        if token in text:
            score -= 90
    score -= max(0, len([part for part in parsed.path.split("/") if part]) - 2) * 3
    return score


def _style_image_url(style: str) -> str:
    match = re.search(r"(?:background(?:-image)?\s*:[^;]*?)url\(\s*['\"]?([^)'\"]+)", style, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _is_probable_html_page(url: str) -> bool:
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    return suffix not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg", ".pdf", ".zip", ".mp4", ".mov"}


def _is_direct_image_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme in {"http", "https"} and Path(parsed.path).suffix.casefold() in {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".avif",
    }


def _website_image_identity(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    path = re.sub(r"-(\d{2,5})x(\d{2,5})(?=\.[a-z0-9]{2,5}$)", "", parsed.path, flags=re.IGNORECASE)
    return urlunparse((parsed.scheme.casefold(), parsed.netloc.casefold(), path, "", "", ""))


def _best_website_candidates(candidates: Iterable[WebsiteImageCandidate]) -> list[WebsiteImageCandidate]:
    selected: dict[str, tuple[int, int, WebsiteImageCandidate]] = {}
    for order, candidate in enumerate(candidates):
        identity = _website_image_identity(candidate.image_url)
        if not identity:
            continue
        parsed = urlparse(candidate.image_url)
        query_width = max((_safe_int(value) for key, value in _query_pairs(parsed.query) if key.casefold() in {"w", "width"}), default=0)
        sized_variant = bool(re.search(r"-\d{2,5}x\d{2,5}(?=\.[a-z0-9]{2,5}$)", parsed.path, flags=re.IGNORECASE))
        score = min(candidate.width, 20_000) + min(candidate.height, 20_000)
        score += min(query_width, 10_000) * 2
        score += 600 if candidate.image_url != candidate.thumb_url else 0
        score += 300 if candidate.dimensions_verified else 0
        score += 100 if candidate.alt else 0
        score -= 500 if sized_variant else 0
        current = selected.get(identity)
        if current is None or score > current[0]:
            selected[identity] = (score, order, candidate)
    return [entry[2] for entry in sorted(selected.values(), key=lambda entry: entry[1])]


def _query_pairs(query: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for part in query.split("&"):
        key, separator, value = part.partition("=")
        if separator:
            pairs.append((unquote(key), unquote(value)))
    return pairs


def _expected_dynamic_image_count(text: str) -> int:
    values = _nuxt_data_values(text)
    if not values:
        return 0
    counts: list[int] = []
    for value in values:
        if not isinstance(value, dict) or "galleryThumbnailCount" not in value:
            continue
        resolved = _nuxt_reference(values, value.get("galleryThumbnailCount"))
        if isinstance(resolved, int) and resolved > 0:
            counts.append(resolved)
    return max(counts, default=0)


def _declared_project_collections(text: str) -> dict[str, tuple[str, int]]:
    values = _nuxt_data_values(text)
    collections: dict[str, tuple[str, int]] = {}
    for value in values:
        if not isinstance(value, dict) or "imageCount" not in value:
            continue
        content_type = _nuxt_reference(values, value.get("_type"))
        if content_type != "project":
            continue
        count = _nuxt_reference(values, value.get("imageCount"))
        if not isinstance(count, int) or count <= 0:
            continue
        collection_id = str(_nuxt_reference(values, value.get("_id")) or "").strip()
        title = str(_nuxt_reference(values, value.get("title")) or "Untitled series").strip()
        if not collection_id:
            collection_id = f"title:{title.casefold()}"
        current = collections.get(collection_id)
        if current is None or count > current[1]:
            collections[collection_id] = (title, count)
    return collections


def _nuxt_data_values(text: str) -> list[object]:
    match = re.search(
        r"<script[^>]+id=[\"']__NUXT_DATA__[\"'][^>]*>(.*?)</script>",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return []
    try:
        values = json.loads(html.unescape(match.group(1)))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return values if isinstance(values, list) else []


def _nuxt_reference(values: list[object], reference: object) -> object:
    if isinstance(reference, int) and 0 <= reference < len(values):
        return values[reference]
    return reference


def _strip_tracking_fragment(url: str) -> str:
    return url.split("#", 1)[0].strip()


def _best_srcset_url(srcset: str) -> str:
    best_url = ""
    best_width = -1
    for part in srcset.split(","):
        tokens = part.strip().split()
        if not tokens:
            continue
        url = tokens[0]
        width = 0
        if len(tokens) > 1:
            match = re.search(r"(\d+)w", tokens[1])
            if match:
                width = int(match.group(1))
        if width >= best_width:
            best_url = url
            best_width = width
    return best_url


def _dimensions_from_attrs(attrs: dict[str, str]) -> tuple[int, int]:
    raw = _first_non_empty(attrs.get("data-image-dimensions"), attrs.get("data-dimensions"))
    match = re.search(r"(\d+)\s*x\s*(\d+)", raw)
    if match:
        return int(match.group(1)), int(match.group(2))
    return _safe_int(attrs.get("width")), _safe_int(attrs.get("height"))


def _intrinsic_dimensions_from_attrs(attrs: dict[str, str]) -> tuple[int, int, bool]:
    raw = _first_non_empty(attrs.get("data-image-dimensions"), attrs.get("data-dimensions"))
    match = re.search(r"(\d+)\s*x\s*(\d+)", raw)
    if match:
        return int(match.group(1)), int(match.group(2)), True
    width = _safe_int(_first_non_empty(attrs.get("data-natural-width"), attrs.get("data-width")))
    height = _safe_int(_first_non_empty(attrs.get("data-natural-height"), attrs.get("data-height")))
    return width, height, bool(width and height)


def _image_dimensions_from_url(url: str) -> tuple[int, int]:
    match = re.search(r"-(\d{2,5})x(\d{2,5})(?=\.[a-z0-9]{2,5}$)", urlparse(url).path, flags=re.IGNORECASE)
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


def _is_decorative_website_image(record: PhotoRecord, search_name: str) -> bool:
    path = urlparse(record.image_url).path.lower()
    title = re.sub(r"\s+", " ", record.title.lower()).strip()
    name = re.sub(r"\s+", " ", search_name.lower()).strip()
    if any(token in path for token in ("favicon", "logo", "sprite", "placeholder", "transparent", "piwik", "tracking")):
        return True
    if Path(path).suffix.lower() == ".svg":
        return True
    if not record.long_edge and title == name:
        return True
    return False


def _get_json_with_retries(
    session: requests.Session,
    url: str,
    params: dict[str, object],
    callback: Optional[ProgressCallback],
    cancel_event: Optional[Event] = None,
) -> dict:
    last_error: Optional[Exception] = None
    for attempt in range(API_RETRIES + 1):
        _check_cancel(cancel_event)
        try:
            response = session.get(url, params=params, timeout=(8, 25))
            retry_after = _safe_int(response.headers.get("Retry-After"))
            if response.status_code in {429, 503} and retry_after:
                _emit(callback, "source_retry", message=f"Server asked to retry after {retry_after}s.", retry_after=retry_after)
                _wait_or_cancel(cancel_event, min(10, max(1, retry_after)))
                continue
            response.raise_for_status()
            data = response.json()
            error = data.get("error") if isinstance(data, dict) else None
            if isinstance(error, dict) and error.get("code") == "maxlag":
                retry_after = _safe_int(response.headers.get("Retry-After")) or 5
                _emit(callback, "source_retry", message=f"Wikimedia is lagged; retrying after {retry_after}s.", retry_after=retry_after)
                _wait_or_cancel(cancel_event, min(10, max(1, retry_after)))
                continue
            return data
        except SearchCancelled:
            raise
        except Exception as exc:
            last_error = exc
            if attempt >= API_RETRIES:
                break
            wait_seconds = 1 + attempt * 2
            _emit(callback, "source_retry", message=f"Network request failed; retrying in {wait_seconds}s: {exc}", retry_after=wait_seconds)
            _wait_or_cancel(cancel_event, wait_seconds)
    raise last_error or PhotoArchiveError("API request failed.")


def _download_binary(
    url: str,
    destination: Path,
    callback: Optional[ProgressCallback] = None,
    title: str = "",
    cancel_event: Optional[Event] = None,
) -> None:
    headers = {"User-Agent": USER_AGENT, "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"}
    try:
        with requests.get(url, headers=headers, stream=True, timeout=(10, 60)) as response:
            response.raise_for_status()
            total = _safe_int(response.headers.get("Content-Length"))
            if total > MAX_IMAGE_DOWNLOAD_BYTES:
                raise PhotoArchiveError(
                    f"Image exceeds the {MAX_IMAGE_DOWNLOAD_BYTES // (1024 * 1024)} MiB download limit."
                )
            done = 0
            with destination.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=512 * 1024):
                    _check_cancel(cancel_event)
                    if not chunk:
                        continue
                    done += len(chunk)
                    if done > MAX_IMAGE_DOWNLOAD_BYTES:
                        raise PhotoArchiveError(
                            f"Image exceeds the {MAX_IMAGE_DOWNLOAD_BYTES // (1024 * 1024)} MiB download limit."
                        )
                    handle.write(chunk)
                    _emit(callback, "bytes", title=title, done=done, total=total)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    _wait_or_cancel(cancel_event, 0.15)


def _destination_for_record(record: PhotoRecord, image_dir: Path) -> Path:
    parsed = urlparse(record.image_url)
    suffix = Path(parsed.path).suffix
    if not suffix or len(suffix) > 8:
        suffix = mimetypes.guess_extension(record.mime or "") or ".jpg"
    name = safe_filename(record.title, "image")
    source_id = safe_filename(record.source_id.replace(":", "_"), "source")
    return image_dir / f"{source_id}_{name}{suffix.lower()}"


def _extract_extmetadata(raw: dict) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, payload in raw.items():
        if isinstance(payload, dict):
            value = payload.get("value", "")
        else:
            value = payload
        result[key] = html_to_text(value)
    return result


def _metadata_list_to_map(raw: list) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        value = item.get("value", "")
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(part) for part in value)
        result[name] = html_to_text(value)
    return result


def _file_title_to_name(value: str) -> str:
    value = value.removeprefix("File:").removeprefix("Image:")
    stem = Path(value).stem if "." in value else value
    return html_to_text(stem.replace("_", " "))


def _first_non_empty(*values: object) -> str:
    for value in values:
        text = html_to_text(value)
        if text:
            return text
    return ""


def _join_non_empty(*values: object) -> str:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = html_to_text(value)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return "\n".join(result)


def _safe_int(value: object) -> int:
    try:
        return int(float(str(value or "0").strip()))
    except (TypeError, ValueError):
        return 0


def _record_value(record: PhotoRecord, column: str) -> object:
    value = getattr(record, column)
    if column in {"match_confidence", "width", "height", "file_size", "duplicate_distance", "rating"}:
        return int(value or 0)
    return value or ""


def _row_to_record(row: sqlite3.Row | None) -> Optional[PhotoRecord]:
    if row is None:
        return None
    values = {column: row[column] for column in PHOTO_COLUMNS}
    values["match_confidence"] = int(values.get("match_confidence") or 0)
    values["width"] = int(values.get("width") or 0)
    values["height"] = int(values.get("height") or 0)
    values["file_size"] = int(values.get("file_size") or 0)
    values["duplicate_distance"] = int(values.get("duplicate_distance") or -1)
    values["rating"] = int(values.get("rating") or 0)
    return PhotoRecord(**values)


def _merge_record(existing: PhotoRecord, incoming: PhotoRecord) -> PhotoRecord:
    for column in [
        "collection_title",
        "local_path",
        "sha256",
        "dhash",
        "duplicate_of",
        "near_duplicate_of",
        "downloaded_at",
    ]:
        if not getattr(incoming, column) and getattr(existing, column):
            setattr(incoming, column, getattr(existing, column))
    for column in ["research_note", "research_tags", "rating"]:
        setattr(incoming, column, getattr(existing, column))
    if incoming.duplicate_distance == -1 and existing.duplicate_distance != -1:
        incoming.duplicate_distance = existing.duplicate_distance
    return incoming


def _check_cancel(cancel_event: Optional[Event]) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise SearchCancelled("Search cancelled by user.")


def _wait_or_cancel(cancel_event: Optional[Event], seconds: float) -> None:
    if cancel_event is not None:
        if cancel_event.wait(max(0.0, seconds)):
            raise SearchCancelled("Search cancelled by user.")
        return
    time.sleep(max(0.0, seconds))


def _emit(callback: Optional[ProgressCallback], event: str, **payload: object) -> None:
    if callback:
        callback(event, payload)
