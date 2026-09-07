# Changelog

## Unreleased

- Made live gallery refreshes deadline-based so a continuous result stream cannot delay the first visible cards.
- Batched and bounded activity-log rendering to keep large portfolio crawls responsive.
- Restored the previous photographer, archive directory, retrieval options, and preferred result view on launch.
- Bound restored and manually entered official-site URLs to their photographer so a later name change cannot reuse the wrong site.
- Made unexpected interface errors non-blocking while retaining diagnostics in the activity log.
- Reduced large-library reset work and added clearer searching and empty-result states.

## 1.0.0 - 2026-07-18

- Added verified official-site discovery with bounded same-origin gallery crawling.
- Added live visual results, remote previews, local archive browsing, comparison, contact sheets, notes, tags, and ratings.
- Added exact SHA-256 and perceptual dHash duplicate detection.
- Added resilient per-page and per-record error handling with cancellable progress.
- Added explicit SQLite connection cleanup and a 512 MiB per-image safety limit.
- Added public-address enforcement, per-hop redirect checks, connected-peer validation, and a deny-by-default dynamic browser network boundary.
- Added graceful active-task shutdown and atomic separation between metadata refreshes and downloaded-file state.
- Added reproducible Windows packaging, hash-locked dependencies, complete third-party notices, privacy scanning, and least-privilege CI.
