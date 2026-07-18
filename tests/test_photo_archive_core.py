from __future__ import annotations

import queue
import sqlite3
import threading
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

import photo_archive_app
import photo_archive_core
from photo_archive_app import (
    GALLERY_PAGE_SIZE,
    GALLERY_REFLOW_DELAY_MS,
    MAX_THUMBNAIL_CACHE_ITEMS,
    THUMBNAIL_WORKERS,
    UI_EVENTS_PER_TICK,
    UI_TICK_BUDGET_SECONDS,
    _DaemonTaskPool,
    _decode_study_image,
    _gallery_page_window,
    _record_matches_scope,
)
from photo_archive_core import (
    MAX_IMAGE_DOWNLOAD_BYTES,
    ArchiveStore,
    BrowserDOMRenderer,
    PhotoRecord,
    SearchCancelled,
    SearchResult,
    WebsiteImageSource,
    WikimediaCommonsSource,
    _download_binary,
    archive_photographer,
    create_contact_sheet_pages,
    dhash_file,
    discover_official_website,
    download_record,
    extract_exif_details,
    find_near_duplicate,
    hamming_distance_hex,
    html_to_text,
    normalize_research_tags,
    render_research_markdown,
    safe_filename,
    sha256_file,
)


@pytest.fixture(autouse=True)
def resolve_reserved_test_hosts(monkeypatch: pytest.MonkeyPatch):
    real_getaddrinfo = photo_archive_core.socket.getaddrinfo

    def getaddrinfo(host, port, *args, **kwargs):
        if str(host).casefold().endswith(".test"):
            return [
                (
                    photo_archive_core.socket.AF_INET,
                    photo_archive_core.socket.SOCK_STREAM,
                    6,
                    "",
                    ("93.184.216.34", port or 0),
                )
            ]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(photo_archive_core.socket, "getaddrinfo", getaddrinfo)


def test_archive_store_explicitly_closes_every_connection(tmp_path: Path, monkeypatch) -> None:
    opened: list[sqlite3.Connection] = []
    closed: list[sqlite3.Connection] = []

    class TrackingConnection(sqlite3.Connection):
        def close(self) -> None:
            closed.append(self)
            super().close()

    original_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        connection = original_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(photo_archive_core.sqlite3, "connect", tracking_connect)
    store = ArchiveStore(tmp_path / "archive.db")
    store.list_records()

    assert opened
    assert len(closed) == len(opened)


def test_download_binary_rejects_declared_oversized_image(tmp_path: Path, monkeypatch) -> None:
    class OversizedResponse:
        headers = {"Content-Length": str(MAX_IMAGE_DOWNLOAD_BYTES + 1)}
        status_code = 200
        url = "https://example.test/oversized.jpg"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(photo_archive_core, "_send_pinned_get", lambda *_args, **_kwargs: OversizedResponse())
    destination = tmp_path / "oversized.part"

    with pytest.raises(photo_archive_core.PhotoArchiveError, match="512 MiB"):
        _download_binary("https://example.test/oversized.jpg", destination)

    assert not destination.exists()


def test_release_smoke_validates_packaged_resources() -> None:
    assert photo_archive_app.main(["--release-smoke"]) == 0


def test_ui_event_pump_reschedules_after_handler_failure() -> None:
    events: queue.Queue[tuple[str, dict]] = queue.Queue()
    events.put(("broken", {}))
    events.put(("healthy", {}))
    handled: list[str] = []
    errors: list[BaseException] = []
    scheduled: list[int] = []

    def handle(event: str, _payload: dict) -> None:
        if event == "broken":
            raise RuntimeError("event failed")
        handled.append(event)

    harness = SimpleNamespace(
        ui_queue=events,
        _handle_event=handle,
        report_callback_exception=lambda _type, value, _traceback: errors.append(value),
        winfo_exists=lambda: True,
        after=lambda delay, _callback: scheduled.append(delay),
        _drain_queue=lambda: None,
    )

    photo_archive_app.PhotoArchiveApp._drain_queue(harness)

    assert handled == ["healthy"]
    assert len(errors) == 1
    assert scheduled


def test_website_fetch_rejects_oversized_html_before_parsing(monkeypatch) -> None:
    class Response:
        headers = {
            "Content-Type": "text/html",
            "Content-Length": str(photo_archive_core.MAX_WEBSITE_HTML_BYTES + 1),
        }
        encoding = "utf-8"
        url = "https://example.test/gallery"

        def raise_for_status(self) -> None:
            return None

        def close(self) -> None:
            return None

    class Session:
        headers: dict[str, str] = {}

        def get(self, _url: str, **_kwargs):
            return Response()

    source = WebsiteImageSource(session=Session())
    with pytest.raises(photo_archive_core.PhotoArchiveError, match="response limit"):
        source._fetch_html("https://example.test/gallery")


def test_website_fetch_rejects_cross_site_redirect() -> None:
    class Response:
        headers = {"Content-Type": "text/html"}
        encoding = "utf-8"
        content = b"<html></html>"
        url = "https://unrelated.test/landing"

        def raise_for_status(self) -> None:
            return None

        def close(self) -> None:
            return None

    class Session:
        headers: dict[str, str] = {}

        def get(self, _url: str, **_kwargs):
            return Response()

    source = WebsiteImageSource(session=Session())
    with pytest.raises(photo_archive_core.PhotoArchiveError, match="redirected outside"):
        source._fetch_html("https://example.test/gallery")


def test_website_fetch_does_not_visit_cross_site_redirect_target() -> None:
    requested: list[str] = []

    class Response:
        headers = {"Location": "http://127.0.0.1/private"}
        status_code = 302
        url = "https://example.test/gallery"

        def close(self) -> None:
            return None

    class Session:
        headers: dict[str, str] = {}

        def get(self, url: str, **_kwargs):
            requested.append(url)
            return Response()

    source = WebsiteImageSource(session=Session())
    with pytest.raises(photo_archive_core.PhotoArchiveError, match="non-public network destination"):
        source._fetch_html("https://example.test/gallery")

    assert requested == ["https://example.test/gallery"]


def test_website_fetch_rejects_private_host_before_request() -> None:
    class Session:
        headers: dict[str, str] = {}

        def get(self, *_args, **_kwargs):
            raise AssertionError("private destination must not be requested")

    source = WebsiteImageSource(session=Session())
    with pytest.raises(photo_archive_core.PhotoArchiveError, match="non-public network destination"):
        source._fetch_html("http://127.0.0.1/private")


def test_download_rejects_private_redirect_before_target_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []

    class RedirectResponse:
        headers = {"Location": "http://127.0.0.1/private.jpg"}
        status_code = 302
        url = "https://example.test/image.jpg"

        def close(self) -> None:
            return None

    def get(_session, url: str, **_kwargs):
        requested.append(url)
        return RedirectResponse()

    monkeypatch.setattr(photo_archive_core, "_send_pinned_get", get)
    with pytest.raises(photo_archive_core.PhotoArchiveError, match="non-public network destination"):
        _download_binary("https://example.test/image.jpg", tmp_path / "image.part")

    assert requested == ["https://example.test/image.jpg"]


def test_public_request_rejects_private_connected_peer() -> None:
    class Socket:
        def getpeername(self):
            return ("127.0.0.1", 443)

    class Raw:
        _connection = SimpleNamespace(sock=Socket())

        def close(self) -> None:
            return None

        def release_conn(self) -> None:
            return None

    response = photo_archive_core.requests.Response()
    response.status_code = 200
    response.url = "https://example.test/image.jpg"
    response.raw = Raw()

    class Session:
        def get(self, *_args, **_kwargs):
            return response

    with pytest.raises(photo_archive_core.PhotoArchiveError, match="non-public network destination"):
        photo_archive_core._request_public_response(
            Session(),
            "https://example.test/image.jpg",
            timeout=(5, 10),
        )


def test_pinned_adapter_connects_to_validated_ip_with_original_tls_identity() -> None:
    adapter = photo_archive_core._PinnedAddressAdapter(
        "93.184.216.34",
        "example.test",
        "example.test",
    )
    request = photo_archive_core.requests.Request(
        "GET",
        "https://example.test/gallery",
    ).prepare()

    adapter.add_headers(request)
    pool = adapter.get_connection_with_tls_context(request, True, proxies={}, cert=None)

    assert request.headers["Host"] == "example.test"
    assert pool.host == "93.184.216.34"
    assert pool.assert_hostname == "example.test"
    assert pool.conn_kw["server_hostname"] == "example.test"
    adapter.close()


def test_network_validation_uses_requests_uts46_idn_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved: list[str] = []

    def resolve(host: str) -> tuple[str, ...]:
        resolved.append(host)
        return ("93.184.216.34",)

    monkeypatch.setattr(photo_archive_core, "_resolve_public_addresses", resolve)
    host, addresses = photo_archive_core._validate_public_url("https://faß.de/gallery")

    assert host == "xn--fa-hia.de"
    assert addresses == ("93.184.216.34",)
    assert resolved == ["xn--fa-hia.de"]
    assert photo_archive_core._origin_prefix("https://faß.de/gallery") == "https://xn--fa-hia.de/"
    assert photo_archive_core._host_header("https://faß.de/gallery") == "xn--fa-hia.de"
    assert photo_archive_core._same_site_host("faß.de", "xn--fa-hia.de")


def test_same_site_host_compares_complete_ipv6_addresses() -> None:
    assert photo_archive_core._same_site_host("2001:4860:4860::8888", "[2001:4860:4860::8888]")
    assert not photo_archive_core._same_site_host("2001:4860:4860::8888", "2001:4860:4860::8844")


def test_public_request_does_not_reresolve_hostname_during_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    resolutions: list[str] = []
    observed: dict[str, object] = {}

    def getaddrinfo(host, port, *args, **kwargs):
        resolutions.append(str(host))
        address = "93.184.216.34" if len(resolutions) == 1 else "127.0.0.1"
        return [(photo_archive_core.socket.AF_INET, photo_archive_core.socket.SOCK_STREAM, 6, "", (address, 0))]

    class Response:
        headers: dict[str, str] = {}
        status_code = 200
        url = "https://rebind.test/gallery"

        def raise_for_status(self) -> None:
            return None

        def close(self) -> None:
            return None

    def get(session, url: str, **_kwargs):
        adapter = session.get_adapter(url)
        observed["address"] = getattr(adapter, "address", "")
        return Response()

    monkeypatch.setattr(photo_archive_core.socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(photo_archive_core, "_send_pinned_get", get)
    session = photo_archive_core.requests.Session()
    response = photo_archive_core._request_public_response(
        session,
        "https://rebind.test/gallery",
        timeout=(5, 10),
    )
    photo_archive_core._close_response(response)
    session.close()

    assert resolutions == ["rebind.test"]
    assert observed["address"] == "93.184.216.34"


@pytest.mark.parametrize(
    "url",
    [
        "https://rebind.test:443/gallery",
        "https://rebind.test./gallery",
    ],
)
def test_pinned_adapter_covers_noncanonical_public_url_origins(
    url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class Response:
        headers: dict[str, str] = {}
        status_code = 200

        def __init__(self, final_url: str) -> None:
            self.url = final_url

        def raise_for_status(self) -> None:
            return None

        def close(self) -> None:
            return None

    def get(session, requested_url: str, **_kwargs):
        observed["adapter"] = session.get_adapter(
            photo_archive_core.requests.Request("GET", requested_url).prepare().url
        )
        return Response(requested_url)

    monkeypatch.setattr(photo_archive_core, "_send_pinned_get", get)
    session = photo_archive_core.requests.Session()
    response = photo_archive_core._request_public_response(session, url, timeout=(5, 10))
    photo_archive_core._close_response(response)
    session.close()

    assert isinstance(observed["adapter"], photo_archive_core._PinnedAddressAdapter)


def test_redirect_response_body_is_not_read_before_target_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    class Socket:
        def getpeername(self):
            return ("93.184.216.34", 443)

    class Raw:
        _connection = SimpleNamespace(sock=Socket())

        def __init__(self) -> None:
            self.read_called = False

        def read(self, *_args, **_kwargs):
            self.read_called = True
            raise AssertionError("redirect body must not be consumed")

        def close(self) -> None:
            return None

        def release_conn(self) -> None:
            return None

    raw = Raw()
    response = photo_archive_core.requests.Response()
    response.status_code = 302
    response.url = "https://example.test/start"
    response.headers["Location"] = "http://127.0.0.1/private"
    response.raw = raw

    def send(_session, url: str, **_kwargs):
        sent.append(url)
        return response

    monkeypatch.setattr(photo_archive_core, "_send_pinned_get", send)
    with pytest.raises(photo_archive_core.PhotoArchiveError, match="non-public network destination"):
        photo_archive_core._request_public_response(
            photo_archive_core.requests.Session(),
            "https://example.test/start",
            timeout=(5, 10),
        )

    assert sent == ["https://example.test/start"]
    assert raw.read_called is False


def test_public_request_disables_environment_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    class Response:
        headers: dict[str, str] = {}
        status_code = 200
        url = "https://example.test/data"

        def raise_for_status(self) -> None:
            return None

        def close(self) -> None:
            return None

    def get(session, _url: str, **kwargs):
        observed["trust_env"] = session.trust_env
        observed.update(kwargs)
        return Response()

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setattr(photo_archive_core, "_send_pinned_get", get)
    session = photo_archive_core.requests.Session()
    response = photo_archive_core._request_public_response(
        session,
        "https://example.test/data",
        params={"format": "json"},
        timeout=(5, 10),
    )
    photo_archive_core._close_response(response)

    assert observed["trust_env"] is False
    assert observed["allow_redirects"] is False
    assert observed["stream"] is True
    assert observed["params"] == {"format": "json"}


def test_public_stream_cancel_closes_a_slow_response(monkeypatch: pytest.MonkeyPatch) -> None:
    started = threading.Event()
    closed = threading.Event()
    result: dict[str, object] = {}

    class SlowResponse:
        headers: dict[str, str] = {}
        status_code = 200
        url = "https://example.test/slow"

        def raise_for_status(self) -> None:
            return None

        def iter_content(self, _chunk_size: int = 1, **_kwargs):
            started.set()
            closed.wait(10)
            if False:
                yield b""

        def close(self) -> None:
            closed.set()

    monkeypatch.setattr(
        photo_archive_core,
        "_send_pinned_get",
        lambda *_args, **_kwargs: SlowResponse(),
    )
    cancel_event = threading.Event()

    def consume() -> None:
        try:
            with photo_archive_core.open_public_stream(
                "https://example.test/slow",
                cancel_event=cancel_event,
                total_timeout=5,
            ) as response:
                list(response.iter_content(1))
        except BaseException as exc:
            result["error"] = exc

    worker = threading.Thread(target=consume)
    worker.start()
    assert started.wait(1)
    cancel_event.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert closed.is_set()
    assert isinstance(result.get("error"), SearchCancelled)


def test_process_termination_has_no_unbounded_post_kill_wait() -> None:
    class StuckProcess:
        def __init__(self) -> None:
            self.wait_timeouts: list[float] = []
            self.terminated = False
            self.killed = False

        def poll(self):
            return None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            raise photo_archive_core.subprocess.TimeoutExpired("renderer", timeout)

    process = StuckProcess()
    photo_archive_core._terminate_process(process)

    assert process.terminated
    assert process.killed
    assert process.wait_timeouts == [2, 2]


def test_background_task_pool_uses_daemon_workers_and_cancels_pending_tasks() -> None:
    started = threading.Event()
    release = threading.Event()
    pending_ran = threading.Event()
    pool = _DaemonTaskPool(max_workers=1, thread_name_prefix="test-photo-pool")

    def active_task() -> None:
        started.set()
        release.wait(2)

    pool.submit(active_task)
    pool.submit(pending_ran.set)
    assert started.wait(1)
    assert all(thread.daemon for thread in pool._threads)

    pool.shutdown(wait=False, cancel_futures=True)
    release.set()
    for thread in pool._threads:
        thread.join(timeout=1)

    assert all(not thread.is_alive() for thread in pool._threads)
    assert not pending_ran.is_set()


def test_manual_download_worker_is_daemon(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    record = PhotoRecord(
        source="website",
        source_id="manual-download",
        search_name="Example",
        title="Manual download",
        page_url="https://example.test/work/manual",
        image_url="https://example.test/work/manual.jpg",
    )

    def blocked_worker(_records, _output_dir) -> None:
        started.set()
        release.wait(2)

    app = SimpleNamespace(
        skipped_records=set(),
        output_dir_var=SimpleNamespace(get=lambda: str(tmp_path)),
        cancel_event=threading.Event(),
        search_started_at=0.0,
        phase_var=SimpleNamespace(set=lambda _value: None),
        detail_var=SimpleNamespace(set=lambda _value: None),
        _set_busy=lambda _value: None,
        _stop_indeterminate_progress=lambda: None,
        _set_progress=lambda _value: None,
        _download_records_worker=blocked_worker,
        worker_thread=None,
    )

    photo_archive_app.PhotoArchiveApp._start_record_download(app, [record])
    assert started.wait(1)
    assert app.worker_thread.daemon

    release.set()
    app.worker_thread.join(timeout=1)
    assert not app.worker_thread.is_alive()


def test_website_parser_bounds_links_and_candidates() -> None:
    parser = photo_archive_core._WebsiteImageParser(
        "https://example.test/",
        max_links=2,
        max_candidates=2,
    )
    parser.feed(
        "".join(
            f"<a href='/page-{index}'><img src='/image-{index}.jpg' alt='Work {index}'></a>"
            for index in range(8)
        )
    )

    assert len(parser.links) == 2
    assert len(parser.candidates) == 2


def test_website_parser_applies_candidate_limit_to_social_meta_images() -> None:
    parser = photo_archive_core._WebsiteImageParser(
        "https://example.test/",
        max_candidates=2,
    )
    parser.feed(
        "".join(
            f"<meta property='og:image' content='/social-{index}.jpg'>"
            for index in range(20)
        )
    )

    assert len(parser.candidates) == 2


def test_safe_filename_removes_windows_reserved_characters() -> None:
    assert safe_filename('A/B:C*D?"E<F>G|.jpg') == "A_B_C_D__E_F_G_.jpg"


def test_ui_work_is_bounded_per_tick() -> None:
    assert THUMBNAIL_WORKERS == 4
    assert UI_EVENTS_PER_TICK <= 24
    assert UI_TICK_BUDGET_SECONDS <= 0.008
    assert GALLERY_REFLOW_DELAY_MS <= 100
    assert GALLERY_PAGE_SIZE <= 60
    assert MAX_THUMBNAIL_CACHE_ITEMS <= GALLERY_PAGE_SIZE * 3


def test_gallery_page_window_bounds_large_result_sets() -> None:
    keys = [f"image-{index}" for index in range(113)]

    first, first_page, page_count = _gallery_page_window(keys, 0)
    last, last_page, last_page_count = _gallery_page_window(keys, 99)

    assert len(first) == GALLERY_PAGE_SIZE
    assert first_page == 0
    assert page_count == 3
    assert last == keys[GALLERY_PAGE_SIZE * 2 :]
    assert last_page == 2
    assert last_page_count == 3


def test_research_scope_filters_are_direct_and_non_overlapping() -> None:
    record = PhotoRecord(
        source="website",
        source_id="scope-1",
        search_name="Example Photographer",
        title="Study",
        page_url="https://example.test/work/1",
        image_url="https://example.test/work/1.jpg",
        width=2400,
        height=1600,
        local_path="C:/archive/work-1.jpg",
        research_note="Color relationship",
        research_tags="color",
        rating=4,
    )

    assert _record_matches_scope(record, "all")
    assert _record_matches_scope(record, "high_resolution")
    assert _record_matches_scope(record, "downloaded")
    assert _record_matches_scope(record, "rated")
    assert _record_matches_scope(record, "noted")
    assert not _record_matches_scope(record, "unreviewed")


def test_batch_download_uses_complete_filtered_result_set() -> None:
    first = PhotoRecord(
        source="website",
        source_id="filtered-1",
        search_name="Example Photographer",
        title="First",
        page_url="https://example.test/1",
        image_url="https://example.test/1.jpg",
    )
    second = PhotoRecord(
        source="website",
        source_id="filtered-2",
        search_name="Example Photographer",
        title="Second",
        page_url="https://example.test/2",
        image_url="https://example.test/2.jpg",
    )
    records = {first.source_key: first, second.source_key: second}
    received: list[PhotoRecord] = []
    harness = SimpleNamespace(
        _visible_source_keys=lambda: [first.source_key, second.source_key],
        _record_for_source_key=records.get,
        _start_record_download=lambda selected: received.extend(selected),
    )

    photo_archive_app.PhotoArchiveApp._download_current_list(harness)

    assert received == [first, second]


def test_study_image_decode_caps_pixel_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    pillow = pytest.importorskip("PIL.Image")
    source = pillow.new("RGB", (400, 300), "#126B5C")
    buffer = BytesIO()
    source.save(buffer, format="JPEG")
    monkeypatch.setattr(photo_archive_app, "MAX_STUDY_DECODE_PIXELS", 20_000)

    decoded = _decode_study_image(buffer.getvalue())

    assert decoded.width * decoded.height <= 20_000
    decoded.close()


def test_photo_archive_icon_assets_are_packaged_and_transparent() -> None:
    pillow = pytest.importorskip("PIL.Image")
    root = Path(__file__).resolve().parents[1]
    icon_dir = root / "assets" / "photo_archive_icons"
    app_icon = pillow.open(icon_dir / "app_icon_256.png").convert("RGBA")

    assert app_icon.size == (256, 256)
    assert app_icon.getpixel((0, 0))[3] == 0
    assert app_icon.getbbox() is not None
    assert "ISC License" in (icon_dir / "LICENSE.txt").read_text(encoding="utf-8")
    spec = (root / "PhotographerImageArchive.spec").read_text(encoding="utf-8")
    assert "photo_archive_icons" in spec
    assert "photo_archive_app.ico" in spec
    for name in ("maximize", "scan", "columns-2", "layout-grid", "chevron-left", "chevron-right"):
        assert (icon_dir / "png" / f"{name}-dark.png").exists()


def test_contact_sheet_export_paginates_and_keeps_missing_preview(tmp_path: Path) -> None:
    pillow = pytest.importorskip("PIL.Image")
    items: list[tuple[PhotoRecord, bytes | None]] = []
    for index in range(13):
        content = None
        if index != 4:
            image = pillow.new("RGB", (800, 600), (20 + index * 8, 90, 140))
            buffer = BytesIO()
            image.save(buffer, format="JPEG")
            content = buffer.getvalue()
        items.append(
            (
                PhotoRecord(
                    source="website",
                    source_id=str(index),
                    search_name="Example Photographer",
                    title=f"Study work {index}",
                    page_url=f"https://example.test/{index}",
                    image_url=f"https://example.test/{index}.jpg",
                    width=2400,
                    height=1600,
                    match_confidence=95,
                ),
                content,
            )
        )

    paths = create_contact_sheet_pages(items, tmp_path / "contact.jpg", "Example Photographer")

    assert [path.name for path in paths] == ["contact_p01.jpg", "contact_p02.jpg"]
    assert all(path.exists() for path in paths)
    with pillow.open(paths[0]) as page:
        assert page.format == "JPEG"
        assert page.size == (1280, 920)
        assert page.getbbox() is not None


def test_html_to_text_strips_tags_and_unescapes_entities() -> None:
    assert html_to_text("<p>Henri&nbsp;<b>Cartier-Bresson</b><br>Paris</p>") == "Henri Cartier-Bresson\nParis"


def test_website_html_uses_detected_encoding_when_charset_is_missing() -> None:
    class FakeResponse:
        headers = {"Content-Type": "text/html"}
        encoding = "ISO-8859-1"
        apparent_encoding = "utf-8"
        content = "北京，1965".encode("utf-8")

        @property
        def text(self) -> str:
            return self.content.decode(self.encoding)

        def raise_for_status(self) -> None:
            return None

    class FakeSession:
        headers: dict[str, str] = {}

        def get(self, url: str, timeout: tuple[int, int], **_kwargs) -> FakeResponse:
            return FakeResponse()

    source = WebsiteImageSource(session=FakeSession())

    assert source._fetch_html("https://example.test/") == "北京，1965"


def test_website_source_extracts_fullsize_images_and_skips_logo() -> None:
    class FakeResponse:
        headers = {"Content-Type": "text/html"}
        encoding = "utf-8"
        text = """
        <html><body>
          <img data-src="/logo.png" alt="Example Photographer">
          <a href="?itemId=photo-1">
            <img
              data-src="https://images.example.test/photo-1-640.jpg"
              data-image="https://images.example.test/photo-1.jpg"
              data-image-dimensions="2500x1668"
              alt="A person crossing a street">
          </a>
        </body></html>
        """

        def raise_for_status(self) -> None:
            return None

    class FakeSession:
        headers: dict[str, str] = {}

        def get(self, url: str, timeout: tuple[int, int], **_kwargs) -> FakeResponse:
            return FakeResponse()

    result = WebsiteImageSource(session=FakeSession()).search(
        "https://example.test/",
        search_name="Example Photographer",
        limit=5,
        min_long_edge=1080,
    )

    assert len(result.records) == 1
    assert result.records[0].image_url == "https://images.example.test/photo-1.jpg"
    assert result.records[0].thumb_url == "https://images.example.test/photo-1-640.jpg"
    assert result.records[0].page_url == "https://example.test/?itemId=photo-1"
    assert result.records[0].resolution == "2500x1668"


def test_website_source_uses_page_collection_and_figure_caption() -> None:
    class FakeResponse:
        headers = {"Content-Type": "text/html"}
        encoding = "utf-8"

        def __init__(self, text: str) -> None:
            self.text = text
            self.content = text.encode("utf-8")

        def raise_for_status(self) -> None:
            return None

    class FakeSession:
        headers: dict[str, str] = {}

        def get(self, url: str, timeout: tuple[int, int], **_kwargs) -> FakeResponse:
            if url.endswith("robots.txt"):
                return FakeResponse("")
            return FakeResponse(
                """
                <html><head><title>American Prospects | Example Photographer</title></head><body>
                  <figure>
                    <img src="/images/f0a163bc9a1d4f9082d40ee150b6c701-2500x1667.jpg"
                         data-image-dimensions="2500x1667">
                    <figcaption>McLean, Virginia, December 4, 1978</figcaption>
                  </figure>
                </body></html>
                """
            )

    result = WebsiteImageSource(session=FakeSession()).search(
        "https://example.test/american-prospects/",
        search_name="Example Photographer",
        limit=1,
        min_long_edge=1080,
    )

    assert len(result.records) == 1
    assert result.records[0].collection_title == "American Prospects"
    assert result.records[0].title == "McLean, Virginia, December 4, 1978"
    assert result.records[0].annotation == "McLean, Virginia, December 4, 1978"


def test_machine_image_title_detects_hash_with_dimension_suffix() -> None:
    title = "f0a1634bdc3437274b6f0b3162d8206e7aa351c5-10328x8262"

    assert photo_archive_core._looks_like_machine_image_title(title, f"https://images.test/{title}.jpg")


def test_website_source_does_not_apply_thumbnail_display_size_to_linked_original() -> None:
    class FakeResponse:
        headers = {"Content-Type": "text/html"}
        encoding = "utf-8"
        content = b""

        def __init__(self, text: str) -> None:
            self.text = text
            self.content = text.encode("utf-8")

        def raise_for_status(self) -> None:
            return None

    class FakeSession:
        headers: dict[str, str] = {}

        def get(self, url: str, timeout: tuple[int, int], **_kwargs) -> FakeResponse:
            if url.endswith("robots.txt"):
                return FakeResponse("")
            return FakeResponse(
                """
                <a href="/uploads/work-001.jpg">
                  <img src="/uploads/work-001-175x265.jpg" width="175" height="265" alt="Paris, 1953">
                </a>
                """
            )

    result = WebsiteImageSource(session=FakeSession()).search(
        "https://example.test/portfolio/",
        search_name="Example Photographer",
        limit=1,
        min_long_edge=1080,
    )

    assert len(result.records) == 1
    assert result.records[0].image_url == "https://example.test/uploads/work-001.jpg"
    assert result.records[0].thumb_url == "https://example.test/uploads/work-001-175x265.jpg"
    assert result.records[0].resolution == ""
    assert result.skipped_low_resolution == 0
    assert result.unknown_dimensions == 1


def test_website_source_can_recover_fullsize_after_low_resolution_variant() -> None:
    class FakeResponse:
        headers = {"Content-Type": "text/html; charset=utf-8"}
        encoding = "utf-8"

        def __init__(self, text: str) -> None:
            self.text = text
            self.content = text.encode("utf-8")

        def raise_for_status(self) -> None:
            return None

    class FakeSession:
        headers: dict[str, str] = {}

        def get(self, url: str, timeout: tuple[int, int], **_kwargs) -> FakeResponse:
            if url.endswith("robots.txt"):
                return FakeResponse("")
            if url.endswith("/fullsize/"):
                return FakeResponse(
                    "<img src='/images/work.jpg' data-image-dimensions='2400x1600' alt='Recovered fullsize work'>"
                )
            return FakeResponse(
                """
                <a href="/fullsize/">Fullsize gallery</a>
                <img src="/images/work-300x200.jpg" width="300" height="200" alt="Small work preview">
                """
            )

    result = WebsiteImageSource(session=FakeSession()).search(
        "https://example.test/portfolio/",
        search_name="Example Photographer",
        limit=1,
        min_long_edge=1080,
    )

    assert result.skipped_low_resolution == 1
    assert len(result.records) == 1
    assert result.records[0].title == "Recovered fullsize work"
    assert result.records[0].resolution == "2400x1600"


def test_website_source_expands_declared_dynamic_gallery_even_with_static_previews() -> None:
    class FakeResponse:
        headers = {"Content-Type": "text/html"}
        encoding = "utf-8"
        content = b""

        def __init__(self, text: str) -> None:
            self.text = text
            self.content = text.encode("utf-8")

        def raise_for_status(self) -> None:
            return None

    class FakeSession:
        headers: dict[str, str] = {}

        def get(self, url: str, timeout: tuple[int, int], **_kwargs) -> FakeResponse:
            if url.endswith("robots.txt"):
                return FakeResponse("")
            return FakeResponse(
                """
                <script type="application/json" id="__NUXT_DATA__">[{"_id":1,"_type":2,"title":3,"imageCount":4,"galleryThumbnailCount":4},"project-a","project","Series A",3]</script>
                <img data-image="/static-1.jpg" data-image-dimensions="2400x1600" alt="One">
                """
            )

    class FakeRenderer:
        available = True
        calls: list[str] = []

        def render(self, url: str, callback=None, cancel_event=None) -> str:
            self.calls.append(url)
            return """
                <img data-image="/dynamic-1.jpg" data-image-dimensions="2400x1600" alt="One">
                <img data-image="/dynamic-2.jpg" data-image-dimensions="2400x1600" alt="Two">
                <img data-image="/dynamic-3.jpg" data-image-dimensions="2400x1600" alt="Three">
            """

    renderer = FakeRenderer()
    events: list[tuple[str, dict]] = []
    result = WebsiteImageSource(session=FakeSession(), renderer=renderer).search(
        "https://example.test/bodies-of-work/example",
        search_name="Example Photographer",
        limit=3,
        min_long_edge=1080,
        callback=lambda event, payload: events.append((event, payload)),
    )

    assert renderer.calls == ["https://example.test/bodies-of-work/example"]
    assert len(result.records) == 3
    assert {record.title for record in result.records} == {"One", "Two", "Three"}
    assert result.series_count == 1
    assert result.declared_total == 3
    collection_events = [payload for event, payload in events if event == "source_collection"]
    assert len(collection_events) == 1
    assert collection_events[0]["series"] == 1
    assert collection_events[0]["expected"] == 3


def test_nuxt_collection_metadata_deduplicates_projects() -> None:
    values = [
        {"_id": 1, "_type": 2, "title": 3, "imageCount": 4},
        "project-a",
        "project",
        "Series A",
        60,
        {"_id": 1, "_type": 2, "title": 3, "imageCount": 4},
        {"_id": 7, "_type": 2, "title": 8, "imageCount": 9},
        "project-b",
        "Series B",
        116,
    ]
    html = f'<script type="application/json" id="__NUXT_DATA__">{photo_archive_core.json.dumps(values)}</script>'

    collections = photo_archive_core._declared_project_collections(html)

    assert collections == {"project-a": ("Series A", 60), "project-b": ("Series B", 116)}


def test_website_page_priority_keeps_work_pages_ahead_of_editorial_pages() -> None:
    portfolio = photo_archive_core._website_page_priority("https://example.test/bodies-of-work/american-prospects")
    writing = photo_archive_core._website_page_priority("https://example.test/bodies-of-work/american-prospects/writings/essay")
    news = photo_archive_core._website_page_priority("https://example.test/news/exhibition-opening")

    assert portfolio > writing
    assert portfolio > news


def test_website_source_renders_dynamic_dom_only_when_static_page_has_no_images() -> None:
    class FakeResponse:
        headers = {"Content-Type": "text/html"}
        encoding = "utf-8"
        content = b""

        def __init__(self, text: str) -> None:
            self.text = text
            self.content = text.encode("utf-8")

        def raise_for_status(self) -> None:
            return None

    class FakeSession:
        headers: dict[str, str] = {}

        def get(self, url: str, timeout: tuple[int, int], **_kwargs) -> FakeResponse:
            if url.endswith("/robots.txt"):
                return FakeResponse("User-agent: *\nAllow: /")
            return FakeResponse("<html><body><div id='app'></div></body></html>")

    class FakeRenderer:
        available = True

        def __init__(self) -> None:
            self.calls: list[str] = []

        def render(self, url: str, callback=None, cancel_event=None) -> str:
            self.calls.append(url)
            return "<img data-image='/dynamic.jpg' data-image-dimensions='2400x1600' alt='Dynamic work'>"

    renderer = FakeRenderer()
    result = WebsiteImageSource(session=FakeSession(), renderer=renderer).search(
        "https://example.test/",
        "Example Photographer",
        limit=1,
        min_long_edge=1080,
    )

    assert renderer.calls == ["https://example.test/"]
    assert [record.title for record in result.records] == ["Dynamic work"]


def test_website_source_does_not_render_when_static_html_has_work_image() -> None:
    class FakeResponse:
        headers = {"Content-Type": "text/html"}
        encoding = "utf-8"
        content = b""
        text = "<img data-image='/static.jpg' data-image-dimensions='2400x1600' alt='Static work'>"

        def raise_for_status(self) -> None:
            return None

    class FakeSession:
        headers: dict[str, str] = {}

        def get(self, url: str, timeout: tuple[int, int], **_kwargs) -> FakeResponse:
            return FakeResponse()

    class FailingRenderer:
        available = True

        def render(self, url: str, callback=None, cancel_event=None) -> str:
            raise AssertionError("renderer should not be called")

    result = WebsiteImageSource(session=FakeSession(), renderer=FailingRenderer()).search(
        "https://example.test/",
        "Example Photographer",
        limit=1,
        min_long_edge=1080,
    )

    assert [record.title for record in result.records] == ["Static work"]


def test_browser_renderer_command_uses_isolated_profile_and_keeps_sandbox(tmp_path: Path) -> None:
    renderer = BrowserDOMRenderer(executable=tmp_path / "chrome.exe")
    profile = tmp_path / "isolated-profile"
    command = renderer.build_command(profile, "https://example.test/", "http://127.0.0.1:9999")

    assert f"--user-data-dir={profile}" in command
    assert "--virtual-time-budget=10000" in command
    assert "--no-sandbox" not in command
    assert "--proxy-server=http://127.0.0.1:9999" in command
    assert any("MAP * ~NOTFOUND" in argument for argument in command)
    assert any("MAP example.test 93.184.216.34" in argument for argument in command)
    assert any("<-loopback>" in argument for argument in command)
    assert all("cookie" not in argument.casefold() for argument in command)
    assert all("profile-directory" not in argument.casefold() for argument in command)


def test_browser_renderer_blocks_private_url_before_browser_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = tmp_path / "chrome.exe"
    executable.touch()
    launched = False

    def popen(*_args, **_kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("private page must not launch Chromium")

    monkeypatch.setattr(photo_archive_core.subprocess, "Popen", popen)
    events: list[tuple[str, dict]] = []
    result = BrowserDOMRenderer(executable=executable).render(
        "http://127.0.0.1/private",
        callback=lambda event, payload: events.append((event, payload)),
    )

    assert result == ""
    assert not launched
    assert any(event == "source_blocked" and "non-public" in payload["message"] for event, payload in events)


def test_browser_renderer_skips_oversized_dom_without_failing_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = tmp_path / "chrome.exe"
    executable.touch()
    profile = tmp_path / "isolated-profile"
    profile.mkdir()

    class FixedTemporaryDirectory:
        def __enter__(self):
            return str(profile)

        def __exit__(self, *_args):
            return False

    class FakeProcess:
        returncode = 0
        stdout = BytesIO(("<html>" + "x" * 80 + "</html>").encode("utf-8"))
        stderr = BytesIO()

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    def fake_popen(*args, **kwargs):
        assert kwargs["stdout"] == photo_archive_core.subprocess.PIPE
        assert kwargs["stderr"] == photo_archive_core.subprocess.PIPE
        return FakeProcess()

    monkeypatch.setattr(photo_archive_core.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(photo_archive_core.tempfile, "TemporaryDirectory", lambda **_kwargs: FixedTemporaryDirectory())
    monkeypatch.setattr(photo_archive_core, "MAX_RENDERED_DOM_CHARS", 64)
    events: list[tuple[str, dict]] = []

    result = BrowserDOMRenderer(executable=executable).render(
        "https://example.test/gallery",
        callback=lambda event, payload: events.append((event, payload)),
    )

    assert result == ""
    assert any(event == "source_notice" and "DOM limit" in payload["message"] for event, payload in events)
    assert (profile / "rendered-dom.html").stat().st_size == 64


def test_browser_renderer_caps_stderr_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = tmp_path / "chrome.exe"
    executable.touch()
    profile = tmp_path / "isolated-profile"
    profile.mkdir()

    class FixedTemporaryDirectory:
        def __enter__(self):
            return str(profile)

        def __exit__(self, *_args):
            return False

    class FakeProcess:
        returncode = 1
        stdout = BytesIO(b"<html></html>")
        stderr = BytesIO(b"x" * 256)

        def wait(self, timeout=None):
            return 1

        def poll(self):
            return 1

    monkeypatch.setattr(photo_archive_core.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(photo_archive_core.tempfile, "TemporaryDirectory", lambda **_kwargs: FixedTemporaryDirectory())
    monkeypatch.setattr(photo_archive_core, "MAX_RENDERER_LOG_BYTES", 64)

    result = BrowserDOMRenderer(executable=executable).render("https://example.test/gallery")

    assert result == ""
    assert (profile / "renderer-error.log").stat().st_size == 64


def test_discover_official_website_uses_wikidata_official_site() -> None:
    class FakeResponse:
        def __init__(self, data: dict) -> None:
            self._data = data

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._data

    class FakeSession:
        headers: dict[str, str] = {}

        def get(self, _url: str, params: dict, timeout: tuple[int, int], **_kwargs) -> FakeResponse:
            if params["action"] == "wbsearchentities":
                return FakeResponse(
                    {
                        "search": [
                            {
                                "id": "Q123",
                                "label": "Daido Moriyama",
                                "description": "Japanese photographer",
                            }
                        ]
                    }
                )
            return FakeResponse(
                {
                    "entities": {
                        "Q123": {
                            "labels": {"en": {"value": "Daido Moriyama"}},
                            "descriptions": {"en": {"value": "Japanese photographer"}},
                            "claims": {
                                "P856": [
                                    {
                                        "mainsnak": {
                                            "datavalue": {
                                                "value": "https://www.moriyamadaido.com/"
                                            }
                                        }
                                    }
                                ],
                                "P106": [
                                    {
                                        "mainsnak": {
                                            "datavalue": {"value": {"id": "Q33231"}}
                                        }
                                    }
                                ],
                                "P31": [
                                    {
                                        "mainsnak": {
                                            "datavalue": {"value": {"id": "Q5"}}
                                        }
                                    }
                                ],
                            },
                        }
                    }
                }
            )

    events: list[str] = []
    url = discover_official_website(
        "Daido Moriyama",
        session=FakeSession(),
        callback=lambda event, _payload: events.append(event),
    )

    assert url == "https://www.moriyamadaido.com/"
    assert events[0] == "official_site_search"
    assert "official_site_found" in events


def test_discover_official_website_rejects_non_photographer_namesake() -> None:
    class FakeResponse:
        def __init__(self, data: dict) -> None:
            self._data = data

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._data

    class FakeSession:
        headers: dict[str, str] = {}

        def get(self, _url: str, params: dict, timeout: tuple[int, int], **_kwargs) -> FakeResponse:
            if params["action"] == "wbsearchentities":
                return FakeResponse({"search": [{"id": "Q999", "label": "Alex Webb", "description": "business executive"}]})
            return FakeResponse(
                {
                    "entities": {
                        "Q999": {
                            "labels": {"en": {"value": "Alex Webb"}},
                            "descriptions": {"en": {"value": "business executive"}},
                            "claims": {
                                "P856": [{"mainsnak": {"datavalue": {"value": "https://wrong.example/"}}}],
                                "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}],
                            },
                        }
                    }
                }
            )

    events: list[str] = []
    url = discover_official_website(
        "Alex Webb",
        session=FakeSession(),
        callback=lambda event, _payload: events.append(event),
    )

    assert url == ""
    assert "official_site_rejected" in events
    assert events[-1] == "official_site_missing"


def test_commons_source_rejects_general_text_match_without_attribution() -> None:
    class FakeResponse:
        headers: dict[str, str] = {}
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            def page(pageid: int, artist: str, description: str) -> dict:
                return {
                    "pageid": pageid,
                    "title": f"File:work-{pageid}.jpg",
                    "imageinfo": [
                        {
                            "url": f"https://upload.example/{pageid}.jpg",
                            "descriptionurl": f"https://commons.example/{pageid}",
                            "thumburl": f"https://upload.example/{pageid}-thumb.jpg",
                            "width": 2400,
                            "height": 1600,
                            "mime": "image/jpeg",
                            "size": 1234,
                            "extmetadata": {
                                "Artist": {"value": artist},
                                "ImageDescription": {"value": description},
                            },
                        }
                    ],
                }

            return {
                "query": {
                    "pages": [
                        page(1, "Michael Christopher Brown", "Congo project"),
                        page(2, "Someone Else", "Michael Christopher Brown speaking at an event"),
                    ]
                }
            }

    class FakeSession:
        headers: dict[str, str] = {}

        def get(self, _url: str, params: dict, timeout: tuple[int, int], **_kwargs) -> FakeResponse:
            return FakeResponse()

    events: list[str] = []
    result = WikimediaCommonsSource(session=FakeSession()).search(
        "Michael Christopher Brown",
        limit=10,
        min_long_edge=1080,
        callback=lambda event, _payload: events.append(event),
    )

    assert [record.source_id for record in result.records] == ["1"]
    assert result.records[0].match_confidence == 100
    assert result.rejected_irrelevant == 1
    assert "record_rejected" in events


def test_commons_record_filters_private_camera_metadata() -> None:
    page = {
        "pageid": 42,
        "title": "File:example.jpg",
        "categories": [{"title": "Category:Research"}],
        "imageinfo": [
            {
                "url": "https://upload.example/example.jpg",
                "descriptionurl": "https://commons.example/example",
                "thumburl": "https://upload.example/example-thumb.jpg",
                "width": 2400,
                "height": 1600,
                "mime": "image/jpeg",
                "size": 1234,
                "extmetadata": {
                    "Artist": {"value": "Example Photographer"},
                    "CreditLine": {"value": "Example credit"},
                    "GPSLatitude": {"value": "1.234"},
                    "GPSLongitude": {"value": "5.678"},
                    "EXIF:CameraOwnerName": {"value": "private owner"},
                    "Exif.Photo.UserComment": {"value": "private ext comment"},
                },
                "metadata": [
                    {"name": "UserComment", "value": "private note"},
                    {"name": "EXIF:UserComment", "value": "private namespaced note"},
                    {"name": "Exif.Photo.MakerNote", "value": "private maker note"},
                    {"name": "MakerNoteUnknownText", "value": "private vendor maker note"},
                    {"name": "Composite:GPSPosition", "value": "1.234, 5.678"},
                    {"name": "BodySerialNumber", "value": "ABC123"},
                    {"name": "CameraSerialNumber", "value": "CAM456"},
                    {"name": "DeviceSerialNumber", "value": "DEV789"},
                    {"name": "InternalSerialNumber", "value": "INT012"},
                    {"name": "EXIF:LensSerialNumberDecoded", "value": "LENS345"},
                    {"name": "Model", "value": "Research Camera"},
                    {"name": "ImageDescription", "value": "Research description"},
                ],
                "commonmetadata": [
                    {"name": "XPComment", "value": "hidden note"},
                    {"name": "EXIF:XPComment", "value": "hidden namespaced note"},
                ],
            }
        ],
    }

    record = WikimediaCommonsSource()._record_from_page(page, "Example Photographer")

    assert record is not None
    assert record.source_comment == "Example credit"
    assert record.camera_model == "Research Camera"
    raw = record.raw_metadata_json.casefold()
    assert "gps" not in raw
    assert "private note" not in raw
    assert "private namespaced note" not in raw
    assert "private ext comment" not in raw
    assert "private maker note" not in raw
    assert "private vendor maker note" not in raw
    assert "private owner" not in raw
    assert "hidden note" not in raw
    assert "hidden namespaced note" not in raw
    assert "abc123" not in raw
    assert "cam456" not in raw
    assert "dev789" not in raw
    assert "int012" not in raw
    assert "lens345" not in raw
    assert "research camera" in raw
    assert "research description" in raw


def test_archive_uses_auto_discovered_official_site_when_url_is_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_discover(search_name: str, callback=None, cancel_event=None) -> str:
        calls.append(f"discover:{search_name}")
        if callback:
            callback("official_site_found", {"url": "https://example.test/"})
        return "https://example.test/"

    class FakeWebsiteSource:
        def search(
            self,
            website_url: str,
            search_name: str,
            limit: int,
            min_long_edge: int,
            callback=None,
            cancel_event=None,
        ) -> SearchResult:
            calls.append(f"website:{website_url}:{search_name}:{limit}:{min_long_edge}")
            record = PhotoRecord(
                source="website",
                source_id="auto-1",
                search_name=search_name,
                title="Official site image",
                page_url=website_url,
                image_url="https://example.test/image.jpg",
                width=2500,
                height=1667,
            )
            if callback:
                callback(
                    "record_found",
                    {
                        "record": record.__dict__,
                        "title": record.title,
                        "resolution": record.resolution,
                        "accepted": 1,
                        "seen": 1,
                        "skipped": 0,
                        "target": limit,
                    },
                )
            return SearchResult(records=[record], found=1, skipped_low_resolution=0)

    monkeypatch.setattr(photo_archive_core, "discover_official_website", fake_discover)
    monkeypatch.setattr(photo_archive_core, "WebsiteImageSource", FakeWebsiteSource)

    summary = archive_photographer(
        "Daido Moriyama",
        output_dir=tmp_path,
        website_url="",
        limit=3,
        min_long_edge=1080,
        download=False,
    )

    assert calls == [
        "discover:Daido Moriyama",
        "website:https://example.test/:Daido Moriyama:3:1080",
    ]
    assert summary.saved == 1
    assert summary.records[0].page_url == "https://example.test/"


def test_archive_persists_emitted_record_before_search_is_cancelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_discover(search_name: str, callback=None, cancel_event=None) -> str:
        return "https://example.test/"

    class CancellingWebsiteSource:
        def search(self, website_url: str, search_name: str, limit: int, min_long_edge: int, callback=None, cancel_event=None) -> SearchResult:
            record = PhotoRecord(
                source="website",
                source_id="persisted-first",
                search_name=search_name,
                title="First result",
                page_url=website_url,
                image_url="https://example.test/first.jpg",
                match_confidence=100,
                match_reason="official website",
            )
            callback(
                "record_found",
                {
                    "record": record.__dict__,
                    "title": record.title,
                    "resolution": "",
                    "accepted": 1,
                    "seen": 1,
                    "skipped": 0,
                    "target": limit,
                },
            )
            raise SearchCancelled("stop after first")

    monkeypatch.setattr(photo_archive_core, "discover_official_website", fake_discover)
    monkeypatch.setattr(photo_archive_core, "WebsiteImageSource", CancellingWebsiteSource)

    with pytest.raises(SearchCancelled):
        archive_photographer("Example Photographer", output_dir=tmp_path, download=False)

    records = ArchiveStore(tmp_path / "photo_archive.db").list_records("Example Photographer")
    assert [record.source_id for record in records] == ["persisted-first"]


def test_website_search_cancellation_keeps_already_emitted_record() -> None:
    class FakeResponse:
        headers = {"Content-Type": "text/html"}
        encoding = "utf-8"
        content = b""

        def __init__(self, text: str) -> None:
            self.text = text
            self.content = text.encode("utf-8")

        def raise_for_status(self) -> None:
            return None

    class FakeSession:
        headers: dict[str, str] = {}

        def get(self, url: str, timeout: tuple[int, int], **_kwargs) -> FakeResponse:
            if url.endswith("/robots.txt"):
                return FakeResponse("User-agent: *\nAllow: /")
            return FakeResponse(
                """
                <img data-image="/one.jpg" data-image-dimensions="2400x1600" alt="One">
                <img data-image="/two.jpg" data-image-dimensions="2400x1600" alt="Two">
                """
            )

    cancel_event = threading.Event()
    found: list[str] = []

    def callback(event: str, payload: dict) -> None:
        if event == "record_found":
            found.append(str(payload["title"]))
            cancel_event.set()

    with pytest.raises(SearchCancelled):
        WebsiteImageSource(session=FakeSession()).search(
            "https://example.test/",
            "Example Photographer",
            limit=5,
            min_long_edge=1080,
            callback=callback,
            cancel_event=cancel_event,
        )

    assert found == ["One"]


def test_sha256_file_is_content_based(tmp_path: Path) -> None:
    left = tmp_path / "left.bin"
    right = tmp_path / "right.bin"
    left.write_bytes(b"same image bytes")
    right.write_bytes(b"same image bytes")

    assert sha256_file(left) == sha256_file(right)


def test_dhash_detects_near_duplicate(tmp_path: Path) -> None:
    pillow = pytest.importorskip("PIL.Image")
    base = tmp_path / "base.jpg"
    changed = tmp_path / "changed.jpg"

    image = pillow.new("RGB", (80, 80), "white")
    for x in range(20, 60):
        for y in range(20, 60):
            image.putpixel((x, y), (20, 20, 20))
    image.save(base)

    image.putpixel((3, 3), (250, 250, 250))
    image.save(changed)

    base_hash = dhash_file(base)
    changed_hash = dhash_file(changed)

    assert base_hash
    assert changed_hash
    assert hamming_distance_hex(base_hash, changed_hash) <= 2


def test_extract_exif_details_reads_shooting_metadata_without_gps(tmp_path: Path) -> None:
    pillow = pytest.importorskip("PIL.Image")
    path = tmp_path / "with-exif.jpg"
    image = pillow.new("RGB", (32, 24), "white")
    exif = pillow.Exif()
    exif[271] = "Leica Camera AG"
    exif[272] = "LEICA Q2"
    exif[36867] = "2024:01:02 03:04:05"
    exif[34855] = 400
    exif[34853] = {1: "private location"}
    image.save(path, exif=exif)

    details = extract_exif_details(path)

    assert details["camera_make"] == "Leica Camera AG"
    assert details["camera_model"] == "LEICA Q2"
    assert details["shooting_date"] == "2024:01:02 03:04:05"
    assert details["iso"] == "400"
    assert "gps" not in " ".join(details).casefold()


def test_find_near_duplicate_uses_dhash_not_title() -> None:
    record = PhotoRecord(
        source="wikimedia_commons",
        source_id="2",
        search_name="Example",
        title="Different title",
        page_url="https://example.test/2",
        image_url="https://example.test/2.jpg",
        dhash="ffff0000ffff0000",
    )
    candidate = PhotoRecord(
        source="wikimedia_commons",
        source_id="1",
        search_name="Example",
        title="Original",
        page_url="https://example.test/1",
        image_url="https://example.test/1.jpg",
        dhash="ffff0000ffff0001",
    )

    result = find_near_duplicate(record, [candidate], max_distance=1)

    assert result is not None
    assert result[0].source_id == "1"
    assert result[1] == 1


def test_archive_store_preserves_download_fields_on_metadata_refresh(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path / "photo_archive.db")
    downloaded = PhotoRecord(
        source="wikimedia_commons",
        source_id="1",
        search_name="Example",
        title="First title",
        page_url="https://example.test/1",
        image_url="https://example.test/1.jpg",
        local_path=str(tmp_path / "image.jpg"),
        sha256="abc",
        dhash="ff",
        downloaded_at="2026-01-01T00:00:00+00:00",
    )
    refreshed = PhotoRecord(
        source="wikimedia_commons",
        source_id="1",
        search_name="Example",
        title="Updated title",
        page_url="https://example.test/1",
        image_url="https://example.test/1.jpg",
    )

    store.upsert(downloaded)
    result = store.upsert(refreshed)

    assert result.title == "Updated title"
    assert result.local_path == downloaded.local_path
    assert result.sha256 == "abc"


def test_archive_store_rejects_stale_nonempty_download_state(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path / "photo_archive.db")
    current = PhotoRecord(
        source="website",
        source_id="download-race-1",
        search_name="Example",
        title="Current title",
        page_url="https://example.test/work/1",
        image_url="https://example.test/work/1.jpg",
        width=2500,
        height=1667,
        local_path=str(tmp_path / "current.jpg"),
        sha256="current-sha",
        dhash="current-dhash",
        downloaded_at="2026-07-18T12:00:00+00:00",
    )
    store.update_download_state(current)

    stale = PhotoRecord(
        source="website",
        source_id="download-race-1",
        search_name="Example",
        title="Refreshed metadata",
        page_url="https://example.test/work/1",
        image_url="https://example.test/work/1.jpg",
        width=640,
        height=427,
        local_path=str(tmp_path / "stale.jpg"),
        sha256="stale-sha",
        dhash="stale-dhash",
        downloaded_at="2026-07-18T11:00:00+00:00",
    )
    result = store.upsert(stale)

    assert result.title == "Refreshed metadata"
    assert result.local_path == current.local_path
    assert result.sha256 == "current-sha"
    assert result.dhash == "current-dhash"
    assert result.downloaded_at == current.downloaded_at
    assert (result.width, result.height) == (2500, 1667)


def test_download_rolls_back_new_file_when_database_write_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if photo_archive_core.Image is None:
        pytest.skip("Pillow is unavailable")
    store = ArchiveStore(tmp_path / "photo_archive.db")
    record = PhotoRecord(
        source="website",
        source_id="rollback-1",
        search_name="Example",
        title="Rollback image",
        page_url="https://example.test/work/rollback",
        image_url="https://example.test/work/rollback.jpg",
    )

    def fake_download(_url: str, destination: Path, **_kwargs) -> None:
        photo_archive_core.Image.new("RGB", (32, 24), "white").save(destination, format="JPEG")

    def fail_update(_record: PhotoRecord) -> PhotoRecord:
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(photo_archive_core, "_download_binary", fake_download)
    monkeypatch.setattr(store, "update_download_state", fail_update)

    with pytest.raises(sqlite3.OperationalError, match="database unavailable"):
        download_record(record, tmp_path, store)

    image_dir = tmp_path / "Example" / "images"
    assert list(image_dir.glob("*")) == []


def test_duplicate_database_failure_preserves_preexisting_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if photo_archive_core.Image is None:
        pytest.skip("Pillow is unavailable")
    store = ArchiveStore(tmp_path / "photo_archive.db")
    record = PhotoRecord(
        source="website",
        source_id="duplicate-rollback",
        search_name="Example",
        title="Preexisting image",
        page_url="https://example.test/work/preexisting",
        image_url="https://example.test/work/preexisting.jpg",
    )
    image_dir = tmp_path / "Example" / "images"
    image_dir.mkdir(parents=True)
    destination = photo_archive_core._destination_for_record(record, image_dir)
    canonical = tmp_path / "canonical.jpg"
    for path in (destination, canonical):
        photo_archive_core.Image.new("RGB", (32, 24), "white").save(path, format="JPEG")
    exact = PhotoRecord(
        source="website",
        source_id="canonical",
        search_name="Example",
        title="Canonical image",
        page_url="https://example.test/work/canonical",
        image_url="https://example.test/work/canonical.jpg",
        local_path=str(canonical),
    )

    monkeypatch.setattr(store, "get_by_sha256", lambda *_args, **_kwargs: exact)

    def fail_update(_record: PhotoRecord) -> PhotoRecord:
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(store, "update_download_state", fail_update)

    with pytest.raises(sqlite3.OperationalError, match="database unavailable"):
        download_record(record, tmp_path, store)

    assert destination.is_file()
    assert canonical.is_file()


def test_archive_store_preserves_research_content_on_source_refresh(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path / "photo_archive.db")
    original = PhotoRecord(
        source="website",
        source_id="research-1",
        search_name="Example Photographer",
        title="Original title",
        collection_title="First series",
        page_url="https://example.test/series/",
        image_url="https://example.test/image.jpg",
    )
    store.upsert(original)

    edited = store.update_research_content(
        "website",
        "research-1",
        "The use of scale changes the reading of the foreground.",
        "color，landscape, Color",
        7,
    )
    refreshed = store.upsert(
        PhotoRecord(
            source="website",
            source_id="research-1",
            search_name="Example Photographer",
            title="Corrected source title",
            collection_title="Corrected series",
            page_url="https://example.test/series/",
            image_url="https://example.test/image.jpg",
        )
    )

    assert edited is not None
    assert edited.rating == 5
    assert edited.research_tags == "color, landscape"
    assert refreshed.title == "Corrected source title"
    assert refreshed.collection_title == "Corrected series"
    assert refreshed.research_note.startswith("The use of scale")
    assert refreshed.research_tags == "color, landscape"
    assert refreshed.rating == 5


def test_archive_store_preserves_latest_research_content_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ArchiveStore(tmp_path / "photo_archive.db")
    latest = PhotoRecord(
        source="website",
        source_id="atomic-1",
        search_name="Example Photographer",
        title="Current title",
        page_url="https://example.test/work/1",
        image_url="https://example.test/work/1.jpg",
        research_note="Latest user note",
        research_tags="latest",
        rating=5,
    )
    store.upsert(latest)
    stale = PhotoRecord(
        source="website",
        source_id="atomic-1",
        search_name="Example Photographer",
        title="Stale title",
        page_url="https://example.test/work/1",
        image_url="https://example.test/work/1.jpg",
    )
    real_get = store.get_by_source
    calls = 0

    def stale_then_live(source: str, source_id: str):
        nonlocal calls
        calls += 1
        return stale if calls == 1 else real_get(source, source_id)

    monkeypatch.setattr(store, "get_by_source", stale_then_live)
    refreshed = store.upsert(
        PhotoRecord(
            source="website",
            source_id="atomic-1",
            search_name="Example Photographer",
            title="Refreshed title",
            page_url="https://example.test/work/1",
            image_url="https://example.test/work/1.jpg",
        )
    )

    assert refreshed.title == "Refreshed title"
    assert refreshed.research_note == "Latest user note"
    assert refreshed.research_tags == "latest"
    assert refreshed.rating == 5


def test_archive_store_migrates_existing_database_for_research_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "photo_archive.db"
    integer_columns = {"match_confidence", "width", "height", "file_size", "duplicate_distance"}
    required_columns = {"source", "source_id", "search_name", "title", "page_url", "image_url"}
    old_columns = [
        column
        for column in photo_archive_core.PHOTO_COLUMNS
        if column not in {"collection_title", "research_note", "research_tags", "rating"}
    ]
    definitions = []
    for column in old_columns:
        if column in required_columns:
            definitions.append(f"{column} TEXT NOT NULL")
        elif column in integer_columns:
            default = -1 if column == "duplicate_distance" else 0
            definitions.append(f"{column} INTEGER DEFAULT {default}")
        else:
            definitions.append(f"{column} TEXT DEFAULT ''")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"CREATE TABLE photos (id INTEGER PRIMARY KEY AUTOINCREMENT, {', '.join(definitions)}, "
            "UNIQUE(source, source_id))"
        )

    store = ArchiveStore(db_path)
    with store.connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(photos)").fetchall()}

    assert {"collection_title", "research_note", "research_tags", "rating"} <= columns


def test_research_markdown_contains_source_and_study_content() -> None:
    record = PhotoRecord(
        source="website",
        source_id="1",
        search_name="Example Photographer",
        title="A Study Work",
        collection_title="American Prospects",
        page_url="https://example.test/work/1",
        image_url="https://example.test/work/1.jpg",
        annotation="A roadside scene.",
        research_note="Observe the relationship between color and distance.",
        research_tags="color, distance",
        rating=4,
    )

    markdown = render_research_markdown(record)

    assert normalize_research_tags(" color，distance, Color ") == "color, distance"
    assert "# A Study Work" in markdown
    assert "系列 / 项目：American Prospects" in markdown
    assert "评分：4/5" in markdown
    assert "https://example.test/work/1" in markdown
    assert "Observe the relationship" in markdown


def test_archive_store_can_delete_current_search_cache(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path / "photo_archive.db")
    store.upsert(
        PhotoRecord(
            source="website",
            source_id="1",
            search_name="Wrong Cache",
            title="Image",
            page_url="https://example.test/",
            image_url="https://example.test/image.jpg",
        )
    )

    assert store.delete_search("Wrong Cache") == 1
    assert store.list_records(search_name="Wrong Cache") == []


def test_archive_store_lists_saved_photographers_by_recent_activity(tmp_path: Path) -> None:
    store = ArchiveStore(tmp_path / "photo_archive.db")
    for source_id, name in (("1", "First Photographer"), ("2", "Second Photographer"), ("3", "First Photographer")):
        store.upsert(
            PhotoRecord(
                source="website",
                source_id=source_id,
                search_name=name,
                title=f"Image {source_id}",
                page_url="https://example.test/",
                image_url=f"https://example.test/{source_id}.jpg",
            )
        )

    assert store.list_search_names() == [("First Photographer", 2), ("Second Photographer", 1)]
