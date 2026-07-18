from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from photo_archive_core import (
    DEFAULT_MIN_LONG_EDGE,
    DEFAULT_OUTPUT_DIR,
    ArchiveSummary,
    PhotoArchiveError,
    archive_photographer,
)
from photo_archive_version import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download rights-aware photographer image archives with metadata and deduplication."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("photographer", help="Photographer name, for example: Henri Cartier-Bresson")
    parser.add_argument("--website-url", default="", help="Official website URL to scan before using generic sources.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Archive output directory. Default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument("--limit", type=int, default=40, help="Maximum search records to save.")
    parser.add_argument("--download-limit", type=int, default=None, help="Maximum images to download.")
    parser.add_argument("--min-edge", type=int, default=DEFAULT_MIN_LONG_EDGE, help="Minimum long edge in pixels.")
    parser.add_argument("--metadata-only", action="store_true", help="Save metadata without downloading image files.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary.")
    args = parser.parse_args(argv)

    events: list[dict] = []

    def callback(event: str, payload: dict) -> None:
        events.append({"event": event, **payload})
        if args.json:
            return
        if event == "status":
            print(payload.get("message", ""))
        elif event == "official_site_search":
            print(payload.get("message", ""))
        elif event == "official_site_found":
            print(f"official site: {payload.get('url')}")
        elif event == "official_site_rejected":
            print(payload.get("message", "low-confidence official-site candidate rejected"))
        elif event == "official_site_missing":
            print(payload.get("message", ""))
        elif event in {"source_index", "source_page", "source_notice"}:
            print(payload.get("message", ""))
        elif event == "record_found":
            print(f"found: {payload.get('title')} ({payload.get('resolution')})")
        elif event == "record_rejected":
            print(f"rejected: {payload.get('title')} ({payload.get('reason')})")
        elif event == "download_progress":
            print(f"download {payload.get('current')}/{payload.get('total')}: {payload.get('title')}")
        elif event == "duplicate":
            kind = payload.get("kind")
            distance = payload.get("distance")
            suffix = f", distance={distance}" if distance is not None else ""
            print(f"{kind} duplicate: {payload.get('title')} -> {payload.get('duplicate_of')}{suffix}")
        elif event == "downloaded":
            print(f"saved: {payload.get('path')}")

    try:
        summary = archive_photographer(
            args.photographer,
            output_dir=args.output,
            website_url=args.website_url,
            limit=args.limit,
            download_limit=args.download_limit,
            min_long_edge=args.min_edge,
            download=not args.metadata_only,
            callback=callback,
        )
    except (PhotoArchiveError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    if args.json:
        print(json.dumps(_summary_to_dict(summary, events), ensure_ascii=False, indent=2))
    else:
        print("")
        print(f"archive: {summary.output_dir}")
        print(f"database: {summary.database_path}")
        print(f"records saved: {summary.saved}")
        print(f"images downloaded: {summary.downloaded}")
        print(f"exact duplicates: {summary.exact_duplicates}")
        print(f"near duplicates: {summary.near_duplicates}")
        print(f"skipped below min edge: {summary.skipped_low_resolution}")
    return 0


def _summary_to_dict(summary: ArchiveSummary, events: list[dict]) -> dict:
    return {
        "search_name": summary.search_name,
        "output_dir": str(summary.output_dir),
        "database_path": str(summary.database_path),
        "found": summary.found,
        "saved": summary.saved,
        "downloaded": summary.downloaded,
        "exact_duplicates": summary.exact_duplicates,
        "near_duplicates": summary.near_duplicates,
        "skipped_low_resolution": summary.skipped_low_resolution,
        "records": [
            {
                "title": record.title,
                "resolution": record.resolution,
                "local_path": record.local_path,
                "page_url": record.page_url,
                "match_confidence": record.match_confidence,
                "match_reason": record.match_reason,
                "license": record.license_name,
                "duplicate_of": record.duplicate_of,
                "near_duplicate_of": record.near_duplicate_of,
                "duplicate_distance": record.duplicate_distance,
            }
            for record in summary.records
        ],
        "events": events,
    }


if __name__ == "__main__":
    raise SystemExit(main())
