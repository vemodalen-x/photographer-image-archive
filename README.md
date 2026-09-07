# Photographer Image Archive

A local-first Windows research workspace for discovering a photographer's public portfolio, reviewing works as they arrive, preserving source context, and detecting duplicates. The interface is in Simplified Chinese; source metadata is kept in its original language.

## Install

Download the latest Windows ZIP from [GitHub Releases](https://github.com/vemodalen-x/photographer-image-archive/releases/latest), verify it with the published SHA-256 manifest, extract it, and run `PhotographerImageArchive.exe`.

The portable executable is currently unsigned, so Windows SmartScreen may request confirmation on first launch. The application does not require administrator privileges and does not install browser extensions or services.

For source development:

```powershell
python -m pip install -r requirements-dev.txt
python photo_archive_app.py
```

`photo_archive_core.py`, `photo_archive_cli.py`, and `photo_archive_app.py` provide the local research workflow.

Current source order: automatic official-website discovery first, then Wikimedia Commons fallback.

If the official website URL field is empty, the app looks up the photographer on Wikidata and accepts the official website only when the entity has strong photographer evidence. Low-confidence namesakes are rejected. If a URL is provided manually, the app scans that site directly.

The website scanner respects `robots.txt`, reads declared sitemaps, prioritizes portfolio/project/series/body-of-work pages, and deprioritizes shop/news/legal pages. It extracts Open Graph images, picture sources, CSS background images, thumbnails, `data-src`, `data-image`, lazy-source attributes, `srcset`, wrapped source-page links, dimensions such as `data-image-dimensions`, page-level collection names, and `<figure>/<figcaption>` captions. Human captions are preferred over generated hash filenames. It follows same-origin gallery and series links with bounded page and candidate limits. When a public page declares more works than its static HTML exposes, the scanner uses a locally installed Chrome or Edge browser to execute the gallery's public JavaScript and collect the completed DOM. Each render uses an isolated temporary browser profile and never imports cookies or a signed-in profile.

For Nuxt portfolio sites, public project metadata is decoded to show the number of discovered series and the site's approximate declared image total. These estimates are deduplicated by project ID. Sitemap URLs are ranked as discovery hints: work-bearing pages are scanned before books, writings, press, news, exhibitions, shops, and policy pages. Dynamic DOM output is bounded to 16 MB per rendered page so an unusually large or malformed page is logged and skipped without stopping the remaining crawl.

Wikimedia Commons full-text search is used only for candidate discovery. A file is accepted only when its author, credit, byline, or photographer category strongly matches the requested name. General pages that merely mention the photographer are excluded.

The archive stores:

- title
- collection / project name
- source page URL
- image URL
- local file path
- author / attribution
- license name and license URL
- annotation / caption
- source comments and credit lines
- EXIF-style shooting details when available
- SHA-256 exact hash
- dHash perceptual hash
- exact and near-duplicate relationships
- photographer/source match confidence and match reason
- local study note, normalized tags, and 0-5 rating

Command-line example:

```powershell
python photo_archive_cli.py "Henri Cartier-Bresson" --output .\photo_archive --limit 20 --download-limit 8 --min-edge 1080
```

Leave `--website-url` blank to let the tool auto-detect the official site, or pass `--website-url https://example.com/` to scan a specific site.

Desktop app:

```powershell
python photo_archive_app.py
```

The desktop app includes:

- photographer name search
- local restoration of the last photographer, archive directory, retrieval limits, and gallery/list preference
- recent-photographer history from the selected local archive
- automatic official website lookup when the URL field is blank
- optional manual official website URL scanning
- visual-first responsive gallery with a synchronized metadata list view
- a two-row results command bar that separates browsing, content actions, and filtering
- direct research-state filters for high-resolution, downloaded, rated, noted, and unreviewed works
- a 48-work paged gallery with bounded thumbnail caching for responsive multi-thousand-image archives
- live result filtering across title, collection, author, caption, source comments, study notes, tags, rating, date, camera, and match reason
- keyboard navigation between gallery items, Page Up / Page Down result paging, double-click study view, Enter-to-search from the photographer field, and Escape / F5 commands
- icon command bars, concise tooltips, and right-click record actions
- collapsible advanced retrieval settings and activity log
- live search progress with checked / accepted / skipped counts
- records appear in the table as soon as they are found
- every accepted record is written to SQLite immediately, before the whole search completes
- thumbnails load into the result table while the search is still running
- thumbnail work is capped at four workers, UI event bursts are time-sliced, and live gallery refreshes cannot be postponed by a continuous result stream
- staged progress for identity lookup, site policy, sitemap discovery, page scanning, persistence, and download
- separate progress counts for confirmed low-resolution files, decorative assets, duplicate URL variants, unknown dimensions, and failed pages
- elapsed time, issue count, and a Stop action that preserves partial results
- per-record errors are logged and skipped instead of stopping the whole task; high-volume activity logs are batched and bounded
- remote preview for records that are not downloaded yet
- local preview for downloaded images
- full-screen study viewer with previous/next navigation and fit/original-size modes
- two-slot side-by-side comparison with stable captions and A/B gallery markers
- paginated JPEG contact-sheet export for the current filtered result set
- persistent per-work study notes, tags, and 0-5 ratings that survive later source refreshes
- UTF-8 Markdown research-record export with source attribution, notes, shooting details, and local-file hashes
- selected-image original download for supported public sources
- current-list batch original download for supported public sources
- source page and local file open buttons
- annotations, source comments, license, author, shooting details, hashes, and duplicate status
- EXIF shooting details are recovered from downloaded files when the source did not provide them; GPS/private EXIF is not imported

Existing databases are migrated in place when the app opens; the collection and research fields are added without deleting saved files or metadata. Old Commons records created before match confidence existed remain in the database but are hidden by default. Run a new search to build a verified index, or use `清空当前缓存` to remove all records for the current photographer.

Unknown HTML dimensions are no longer treated as low resolution: those records remain marked `待确认` and their true pixel dimensions are written back after download. Current limitation: some galleries reveal images only after gestures or private API calls, and authenticated social networks may expose no usable public page. The bounded browser fallback does not interact with login prompts, consent walls, endless sessions, or access controls, so those sources may still require opening the source page manually.

For network safety, the dynamic browser fallback only permits the validated official-site host. Galleries that require cross-host scripts may therefore need to be opened manually.

UI iconography is derived from Lucide. Source and release distributions include its ISC/MIT terms in `assets/photo_archive_icons/LICENSE.txt` and `THIRD_PARTY_LICENSES/LUCIDE.txt` respectively.

The default minimum long edge is 1080px. Use `--min-edge 0` if you want to save metadata or images regardless of resolution.

Compliance boundary: this tool does not bypass login, payment, DRM, hotlink protection, access controls, or website restrictions. It downloads from sources that expose files for reuse/download and stores source/license metadata with each record. For copyrighted archives such as Magnum, use this tool to keep metadata and source-page links unless you have download rights for the full-resolution images.
