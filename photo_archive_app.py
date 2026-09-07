from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from photo_archive_core import (
    DEFAULT_MIN_LONG_EDGE,
    DEFAULT_OUTPUT_DIR,
    ArchiveStore,
    PhotoRecord,
    SearchCancelled,
    archive_photographer,
    create_contact_sheet_pages,
    download_record,
    open_public_stream,
    render_research_markdown,
    safe_filename,
)
from photo_archive_version import __version__

try:
    from PIL import Image, ImageOps, ImageTk
except ImportError:  # pragma: no cover - handled at runtime.
    Image = None
    ImageOps = None
    ImageTk = None


APP_TITLE = f"影像研究资料库  {__version__}"
PREVIEW_SIZE = (720, 500)
GRID_THUMB_SIZE = (236, 148)
LIST_THUMB_SIZE = (72, 54)
THUMB_SIZE = GRID_THUMB_SIZE
THUMBNAIL_WORKERS = 4
UI_EVENTS_PER_TICK = 24
UI_TICK_BUDGET_SECONDS = 0.008
LOG_FLUSH_DELAY_MS = 90
MAX_LOG_LINES = 2000
MAX_PREFERENCES_BYTES = 64 * 1024
MAX_THUMBNAIL_BYTES = 16 * 1024 * 1024
MAX_STUDY_IMAGE_BYTES = 64 * 1024 * 1024
MAX_STUDY_CACHE_BYTES = 128 * 1024 * 1024
MAX_STUDY_CACHE_ITEMS = 8
MAX_STUDY_DECODE_PIXELS = 24_000_000
BACKGROUND_SHUTDOWN_GRACE_SECONDS = 15
GALLERY_CARD_WIDTH = 252
GALLERY_CARD_HEIGHT = 220
GALLERY_GAP = 12
GALLERY_REFLOW_DELAY_MS = 70
RESULT_SUMMARY_DELAY_MS = 60
GALLERY_PAGE_SIZE = 48
MAX_THUMBNAIL_CACHE_ITEMS = GALLERY_PAGE_SIZE * 3
RESULT_SCOPE_OPTIONS = (
    ("全部作品", "all"),
    ("高清 1080+", "high_resolution"),
    ("已下载", "downloaded"),
    ("已评分", "rated"),
    ("有笔记", "noted"),
    ("待研究", "unreviewed"),
)
RESULT_SCOPE_CODES = dict(RESULT_SCOPE_OPTIONS)
PREFERENCES_VERSION = 1


def _default_preferences_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local_app_data) if local_app_data else Path.home() / ".photographer-image-archive"
    return base / "PhotographerImageArchive" / "preferences.json" if local_app_data else base / "preferences.json"


DEFAULT_PREFERENCES_PATH = _default_preferences_path()


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _preference_text(payload: dict[str, object], key: str, maximum: int) -> str:
    value = payload.get(key)
    return value.strip()[:maximum] if isinstance(value, str) else ""


def _load_preferences(path: Path | None) -> dict[str, object]:
    if path is None:
        return {}
    try:
        if path.stat().st_size > MAX_PREFERENCES_BYTES:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != PREFERENCES_VERSION:
        return {}

    preferences: dict[str, object] = {
        "photographer": _preference_text(payload, "photographer", 240),
        "website_url": _preference_text(payload, "website_url", 4096),
        "output_dir": _preference_text(payload, "output_dir", 4096),
        "limit": _bounded_int(payload.get("limit"), 40, 1, 5000),
        "download_limit": _bounded_int(payload.get("download_limit"), 12, 0, 5000),
        "min_edge": _bounded_int(payload.get("min_edge"), DEFAULT_MIN_LONG_EDGE, 0, 10000),
        "download": payload.get("download") is True,
        "view_mode": payload.get("view_mode") if payload.get("view_mode") in {"grid", "list"} else "grid",
        "advanced_visible": payload.get("advanced_visible") is True,
        "website_is_auto": payload.get("website_is_auto") is True,
    }
    return preferences


def _write_preferences(path: Path | None, payload: dict[str, object]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class _DaemonTaskPool:
    def __init__(self, max_workers: int, thread_name_prefix: str) -> None:
        self._tasks: queue.Queue[tuple[object, tuple, dict] | None] = queue.Queue()
        self._lock = threading.Lock()
        self._shutdown = False
        self._threads = [
            threading.Thread(
                target=self._run,
                name=f"{thread_name_prefix}_{index}",
                daemon=True,
            )
            for index in range(max(1, max_workers))
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, function, *args, **kwargs) -> None:
        with self._lock:
            if self._shutdown:
                raise RuntimeError("background task pool is shutting down")
            self._tasks.put((function, args, kwargs))

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        with self._lock:
            first_shutdown = not self._shutdown
            self._shutdown = True
        if first_shutdown:
            if cancel_futures:
                while True:
                    try:
                        self._tasks.get_nowait()
                    except queue.Empty:
                        break
                    else:
                        self._tasks.task_done()
            for _thread in self._threads:
                self._tasks.put(None)
        if wait:
            for thread in self._threads:
                thread.join()

    def _run(self) -> None:
        while True:
            task = self._tasks.get()
            try:
                if task is None:
                    return
                function, args, kwargs = task
                function(*args, **kwargs)
            except BaseException:
                traceback.print_exc()
            finally:
                self._tasks.task_done()


def _resource_path(relative_path: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative_path


def _decode_study_image(content: bytes):
    if Image is None:
        raise ValueError("Pillow is required to preview images")
    with Image.open(BytesIO(content)) as source:
        width, height = source.size
        pixels = width * height
        if pixels > MAX_STUDY_DECODE_PIXELS:
            scale = (MAX_STUDY_DECODE_PIXELS / pixels) ** 0.5
            target = (max(1, int(width * scale)), max(1, int(height * scale)))
            source.draft("RGB", target)
            source.thumbnail(target, Image.Resampling.LANCZOS)
        return source.convert("RGB").copy()


def _gallery_page_window(source_keys: list[str], page_index: int, page_size: int = GALLERY_PAGE_SIZE) -> tuple[list[str], int, int]:
    page_size = max(1, int(page_size))
    page_count = max(1, (len(source_keys) + page_size - 1) // page_size)
    page_index = max(0, min(int(page_index), page_count - 1))
    start = page_index * page_size
    return source_keys[start : start + page_size], page_index, page_count


def _record_matches_scope(record: PhotoRecord, scope: str) -> bool:
    if scope == "high_resolution":
        return record.long_edge >= 1080
    if scope == "downloaded":
        return bool(record.local_path)
    if scope == "rated":
        return record.rating > 0
    if scope == "noted":
        return bool(record.research_note.strip() or record.research_tags.strip())
    if scope == "unreviewed":
        return record.rating <= 0 and not record.research_note.strip() and not record.research_tags.strip()
    return True


class IconFactory:
    """Load packaged Lucide PNG variants and retain Tk image references."""

    def __init__(self) -> None:
        self._images: dict[tuple[str, str], object] = {}

    def get(self, name: str, variant: str = "dark") -> object | None:
        key = (name, variant)
        if key in self._images:
            return self._images[key]
        if Image is None or ImageTk is None:
            return None
        path = _resource_path(f"assets/photo_archive_icons/png/{name}-{variant}.png")
        if not path.exists():
            return None
        with Image.open(path) as image:
            photo = ImageTk.PhotoImage(image.convert("RGBA"))
        self._images[key] = photo
        return photo


class ToolTip:
    """Show a short delayed label for icon-only commands."""

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.after_id: str | None = None
        self.window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: object = None) -> None:
        self._cancel()
        self.after_id = self.widget.after(450, self._show)

    def _cancel(self) -> None:
        if self.after_id:
            self.widget.after_cancel(self.after_id)
            self.after_id = None

    def _show(self) -> None:
        if self.window or not self.text:
            return
        x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 7
        window = tk.Toplevel(self.widget)
        window.wm_overrideredirect(True)
        window.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            window,
            text=self.text,
            bg="#20272E",
            fg="#FFFFFF",
            padx=8,
            pady=5,
            font=("Microsoft YaHei UI", 9),
        )
        label.pack()
        window.update_idletasks()
        window.wm_geometry(f"+{x - window.winfo_width() // 2}+{y}")
        self.window = window

    def _hide(self, _event: object = None) -> None:
        self._cancel()
        if self.window:
            self.window.destroy()
            self.window = None


class ResearchEditorDialog(tk.Toplevel):
    def __init__(self, app: "PhotoArchiveApp", record: PhotoRecord, save_callback) -> None:
        super().__init__(app)
        self.record = record
        self.save_callback = save_callback
        self.title("编辑研究内容")
        self.geometry("640x520")
        self.minsize(560, 440)
        self.transient(app)
        self.configure(bg="#F2F4F5")
        self.rating_var = tk.IntVar(value=max(0, min(5, int(record.rating or 0))))
        self.tags_var = tk.StringVar(value=record.research_tags)

        surface = ttk.Frame(self, style="Surface.TFrame", padding=(22, 18))
        surface.pack(fill=tk.BOTH, expand=True, padx=14, pady=14)
        surface.columnconfigure(1, weight=1)
        surface.rowconfigure(4, weight=1)
        ttk.Label(surface, text="研究内容", style="Header.TLabel").grid(row=0, column=0, columnspan=2, sticky=tk.W)
        ttk.Label(
            surface,
            text="  ·  ".join(value for value in (record.collection_title, record.title or "未命名作品") if value),
            style="Muted.TLabel",
            wraplength=540,
        ).grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=(4, 16))

        ttk.Label(surface, text="评分", style="SurfaceText.TLabel").grid(row=2, column=0, sticky=tk.W, padx=(0, 14))
        rating = ttk.Spinbox(surface, from_=0, to=5, textvariable=self.rating_var, width=6, state="readonly")
        rating.grid(row=2, column=1, sticky=tk.W)
        ttk.Label(surface, text="标签", style="SurfaceText.TLabel").grid(row=3, column=0, sticky=tk.W, padx=(0, 14), pady=(14, 0))
        tags = ttk.Entry(surface, textvariable=self.tags_var)
        tags.grid(row=3, column=1, sticky=tk.EW, pady=(14, 0))
        ttk.Label(surface, text="研究笔记", style="SurfaceText.TLabel").grid(row=4, column=0, sticky=tk.NW, padx=(0, 14), pady=(14, 0))
        self.note_text = ScrolledText(
            surface,
            wrap=tk.WORD,
            borderwidth=0,
            font=("Microsoft YaHei UI", 10),
            undo=True,
        )
        self.note_text.grid(row=4, column=1, sticky=tk.NSEW, pady=(14, 0))
        self.note_text.configure(bg="#F6F8F9", fg="#29343C", padx=12, pady=10, relief=tk.FLAT)
        self.note_text.insert("1.0", record.research_note)

        actions = ttk.Frame(surface, style="Surface.TFrame")
        actions.grid(row=5, column=0, columnspan=2, sticky=tk.E, pady=(18, 0))
        ttk.Button(actions, text="取消", command=self.destroy).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(actions, text="保存", style="Accent.TButton", command=self._save).grid(row=0, column=1)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _event: self.destroy())
        self.grab_set()
        self.after_idle(lambda: self._center_on_parent(app))
        self.after_idle(tags.focus_set)

    def _center_on_parent(self, parent: tk.Misc) -> None:
        self.update_idletasks()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_width()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_height()) // 2)
        self.geometry(f"+{x}+{y}")

    def _save(self) -> None:
        saved = self.save_callback(
            self.note_text.get("1.0", tk.END).strip(),
            self.tags_var.get(),
            self.rating_var.get(),
        )
        if saved is not False:
            self.destroy()


class FullscreenStudyViewer(tk.Toplevel):
    def __init__(self, app: "PhotoArchiveApp", records: list[PhotoRecord], index: int) -> None:
        super().__init__(app)
        self.app = app
        self.records = records
        self.index = max(0, min(index, len(records) - 1))
        self.mode = "fit"
        self.source_image = None
        self.tk_image = None
        self.render_after: str | None = None
        self.configure(bg="#11171B")
        self.title("全屏查看")
        try:
            self.attributes("-fullscreen", True)
        except tk.TclError:
            self.state("zoomed")

        bar = tk.Frame(self, bg="#11171B", height=54)
        bar.pack(fill=tk.X)
        bar.pack_propagate(False)
        self.counter_label = tk.Label(bar, bg="#11171B", fg="#AEB8BF", font=("Microsoft YaHei UI", 10))
        self.counter_label.pack(side=tk.LEFT, padx=(18, 10))
        self.title_label = tk.Label(bar, bg="#11171B", fg="#FFFFFF", font=("Microsoft YaHei UI", 11, "bold"), anchor=tk.W)
        self.title_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._button(bar, "chevron-left", "上一张", self.previous).pack(side=tk.LEFT, padx=3)
        self._button(bar, "chevron-right", "下一张", self.next).pack(side=tk.LEFT, padx=3)
        self.fit_button = self._button(bar, "maximize", "适合窗口", lambda: self._set_mode("fit"))
        self.fit_button.pack(side=tk.LEFT, padx=3)
        self.actual_button = self._button(bar, "scan", "原始比例", lambda: self._set_mode("actual"))
        self.actual_button.pack(side=tk.LEFT, padx=3)
        self._button(bar, "x", "关闭", self.destroy).pack(side=tk.LEFT, padx=(3, 14))

        self.canvas = tk.Canvas(self, bg="#11171B", highlightthickness=0, borderwidth=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.status_label = tk.Label(self.canvas, text="正在加载高清预览", bg="#11171B", fg="#AEB8BF", font=("Microsoft YaHei UI", 11))
        self.status_window = self.canvas.create_window(0, 0, window=self.status_label, anchor=tk.CENTER)
        self.canvas.bind("<Configure>", self._schedule_render)
        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("<Left>", lambda _event: self.previous())
        self.bind("<Right>", lambda _event: self.next())
        self.bind("<Key-f>", lambda _event: self._set_mode("fit"))
        self.bind("<Key-1>", lambda _event: self._set_mode("actual"))
        self.after_idle(self.focus_force)
        self._load_current()

    def _button(self, parent: tk.Widget, icon: str, tooltip: str, command) -> tk.Button:
        image = self.app.icons.get(icon, "light")
        button = tk.Button(
            parent,
            image=image,
            text="" if image else tooltip[:2],
            command=command,
            bg="#11171B",
            fg="#FFFFFF",
            activebackground="#29343C",
            activeforeground="#FFFFFF",
            relief=tk.FLAT,
            borderwidth=0,
            padx=9,
            pady=8,
            cursor="hand2",
        )
        ToolTip(button, tooltip)
        return button

    def _load_current(self) -> None:
        if not self.records:
            self.destroy()
            return
        record = self.records[self.index]
        self.source_image = None
        self.tk_image = None
        self.canvas.delete("photo")
        self.status_label.configure(text="正在加载高清预览")
        self.canvas.itemconfigure(self.status_window, state=tk.NORMAL)
        self.counter_label.configure(text=f"{self.index + 1} / {len(self.records)}")
        self.title_label.configure(text=record.title or "未命名作品")
        self.app._request_study_image(record, self._on_image_payload)

    def _on_image_payload(self, payload: dict) -> None:
        if not self.winfo_exists():
            return
        content = payload.get("content")
        if not content or Image is None or ImageTk is None:
            self.status_label.configure(text="无法加载这张图片")
            return
        try:
            self.source_image = _decode_study_image(content)
            self.canvas.itemconfigure(self.status_window, state=tk.HIDDEN)
            self._render()
        except Exception:
            self.status_label.configure(text="图片格式无法预览")

    def _schedule_render(self, event: tk.Event | None = None) -> None:
        if event is not None:
            self.canvas.coords(self.status_window, event.width // 2, event.height // 2)
        if self.render_after:
            self.after_cancel(self.render_after)
        self.render_after = self.after(70, self._render)

    def _render(self) -> None:
        self.render_after = None
        if self.source_image is None or ImageTk is None:
            return
        available = (max(1, self.canvas.winfo_width() - 28), max(1, self.canvas.winfo_height() - 28))
        if self.mode == "fit":
            rendered = ImageOps.contain(self.source_image, available) if ImageOps is not None else self.source_image.copy()
        else:
            rendered = self.source_image.copy()
        self.tk_image = ImageTk.PhotoImage(rendered)
        self.canvas.delete("photo")
        self.canvas.create_image(self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2, image=self.tk_image, anchor=tk.CENTER, tags="photo")

    def _set_mode(self, mode: str) -> None:
        self.mode = mode
        self._render()

    def previous(self) -> None:
        self.index = (self.index - 1) % len(self.records)
        self._load_current()

    def next(self) -> None:
        self.index = (self.index + 1) % len(self.records)
        self._load_current()


class CompareStudyWindow(tk.Toplevel):
    def __init__(self, app: "PhotoArchiveApp", records: list[PhotoRecord]) -> None:
        super().__init__(app)
        self.app = app
        self.records = records[:2]
        self.source_images: list[object | None] = [None, None]
        self.tk_images: list[object | None] = [None, None]
        self.configure(bg="#171D21")
        self.title("作品对比")
        self.geometry("1380x820")
        self.minsize(980, 620)

        bar = tk.Frame(self, bg="#171D21", height=54)
        bar.pack(fill=tk.X)
        bar.pack_propagate(False)
        tk.Label(bar, text="双图研究对比", bg="#171D21", fg="#FFFFFF", font=("Microsoft YaHei UI", 12, "bold")).pack(side=tk.LEFT, padx=18)
        close_image = app.icons.get("x", "light")
        close_button = tk.Button(bar, image=close_image, text="关闭" if close_image is None else "", command=self.destroy, bg="#171D21", activebackground="#29343C", relief=tk.FLAT, borderwidth=0, cursor="hand2")
        close_button.pack(side=tk.RIGHT, padx=14, pady=8)
        ToolTip(close_button, "关闭")

        body = tk.Frame(self, bg="#171D21")
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        body.grid_rowconfigure(0, weight=1)
        for index in range(2):
            body.grid_columnconfigure(index, weight=1, uniform="compare")
        self.canvases: list[tk.Canvas] = []
        self.status_labels: list[tk.Label] = []
        self.status_windows: list[int] = []
        for index, record in enumerate(self.records):
            panel = tk.Frame(body, bg="#20272E")
            panel.grid(row=0, column=index, sticky=tk.NSEW, padx=5)
            panel.grid_rowconfigure(0, weight=1)
            panel.grid_columnconfigure(0, weight=1)
            canvas = tk.Canvas(panel, bg="#20272E", highlightthickness=0, borderwidth=0)
            canvas.grid(row=0, column=0, sticky=tk.NSEW)
            status = tk.Label(canvas, text="正在加载", bg="#20272E", fg="#AEB8BF", font=("Microsoft YaHei UI", 10))
            status_window = canvas.create_window(0, 0, window=status, anchor=tk.CENTER)
            caption = tk.Label(
                panel,
                text=f"{record.title or '未命名作品'}\n{record.resolution or '尺寸待确认'}  ·  {'官网' if record.source == 'website' else '开放来源'}  ·  匹配 {record.match_confidence or 0}%",
                bg="#F6F8F9",
                fg="#1C272E",
                justify=tk.LEFT,
                anchor=tk.W,
                padx=14,
                pady=11,
                font=("Microsoft YaHei UI", 10),
            )
            caption.grid(row=1, column=0, sticky=tk.EW)
            canvas.bind("<Configure>", lambda event, slot=index: self._render_slot(slot, event.width, event.height))
            self.canvases.append(canvas)
            self.status_labels.append(status)
            self.status_windows.append(status_window)
            self.app._request_study_image(record, lambda payload, slot=index: self._on_image_payload(slot, payload))
        self.bind("<Escape>", lambda _event: self.destroy())

    def _on_image_payload(self, slot: int, payload: dict) -> None:
        if not self.winfo_exists():
            return
        content = payload.get("content")
        if not content or Image is None:
            self.status_labels[slot].configure(text="无法加载这张图片")
            return
        try:
            self.source_images[slot] = _decode_study_image(content)
            self.canvases[slot].itemconfigure(self.status_windows[slot], state=tk.HIDDEN)
            self._render_slot(slot)
        except Exception:
            self.status_labels[slot].configure(text="图片格式无法预览")

    def _render_slot(self, slot: int, width: int | None = None, height: int | None = None) -> None:
        canvas = self.canvases[slot]
        width = width or canvas.winfo_width()
        height = height or canvas.winfo_height()
        canvas.coords(self.status_windows[slot], width // 2, height // 2)
        source = self.source_images[slot]
        if source is None or ImageTk is None:
            return
        rendered = ImageOps.contain(source, (max(1, width - 24), max(1, height - 24))) if ImageOps is not None else source.copy()
        self.tk_images[slot] = ImageTk.PhotoImage(rendered)
        canvas.delete("photo")
        canvas.create_image(width // 2, height // 2, image=self.tk_images[slot], anchor=tk.CENTER, tags="photo")


class PhotoArchiveApp(tk.Tk):
    def __init__(self, *, load_archive: bool = True, preferences_path: Path | None = DEFAULT_PREFERENCES_PATH) -> None:
        super().__init__()
        self.preferences_path = Path(preferences_path) if preferences_path is not None else None
        preferences = _load_preferences(self.preferences_path)
        self.title(APP_TITLE)
        self.geometry("1440x920")
        self.minsize(1180, 780)
        self.brand_image = None
        try:
            self.iconbitmap(str(_resource_path("assets/photo_archive_icons/photo_archive_app.ico")))
            if Image is not None and ImageTk is not None:
                with Image.open(_resource_path("assets/photo_archive_icons/app_icon_48.png")) as image:
                    self.brand_image = ImageTk.PhotoImage(image.convert("RGBA"))
                self.iconphoto(True, self.brand_image)
        except (OSError, tk.TclError):
            self.brand_image = None

        self.ui_queue: queue.Queue[tuple[str, dict]] = queue.Queue()
        self.records: dict[str, PhotoRecord] = {}
        self.record_iids: dict[str, str] = {}
        self.skipped_records: set[str] = set()
        self.record_index = 0
        self.preview_image = None
        self.preview_token = 0
        self.thumbnail_images: OrderedDict[str, object] = OrderedDict()
        self.list_thumbnail_images: OrderedDict[str, object] = OrderedDict()
        self.thumbnail_loading: set[str] = set()
        self.thumbnail_executor = _DaemonTaskPool(max_workers=THUMBNAIL_WORKERS, thread_name_prefix="photo-thumb")
        self.preview_executor = _DaemonTaskPool(max_workers=2, thread_name_prefix="photo-preview")
        self.worker_thread: threading.Thread | None = None
        self.shutdown_thread: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.shutdown_event = threading.Event()
        self.closing = False
        self.busy = False
        self.search_started_at = 0.0
        self.issue_count = 0
        self.loaded_search_name = ""
        self.loaded_database_path = ""
        self.active_photographer = ""
        self.auto_website_url = ""
        self.auto_website_name = ""
        self._suppress_website_trace = False
        self.icons = IconFactory()
        self.gallery_cards: dict[str, dict[str, tk.Widget]] = {}
        self.gallery_order: list[str] = []
        self.gallery_page_index = 0
        self.selected_source_key = ""
        self.gallery_reflow_after: str | None = None
        self.result_summary_after: str | None = None
        self.filter_after: str | None = None
        self.log_flush_after: str | None = None
        self.pending_log_lines: list[str] = []
        self.rendered_log_lines = 0
        self.advanced_visible = False
        self.log_visible = False
        self.compare_source_keys: list[str] = []
        self.study_request_index = 0
        self.study_callbacks: dict[int, object] = {}
        self.study_image_cache: OrderedDict[str, bytes] = OrderedDict()
        self.study_cache_bytes = 0
        self.study_cache_lock = threading.Lock()
        self.study_executor = _DaemonTaskPool(max_workers=3, thread_name_prefix="photo-study")

        photographer = str(preferences.get("photographer") or "Michael Christopher Brown")
        website_url = str(preferences.get("website_url") or "")
        self.website_url_name = photographer if website_url else ""
        self.photographer_var = tk.StringVar(value=photographer)
        self.website_url_var = tk.StringVar(value=website_url)
        if website_url and preferences.get("website_is_auto"):
            self.auto_website_url = website_url
            self.auto_website_name = photographer
        self.photographer_var.trace_add("write", self._on_photographer_changed)
        self.website_url_var.trace_add("write", self._on_website_url_changed)
        self.output_dir_var = tk.StringVar(value=str(preferences.get("output_dir") or DEFAULT_OUTPUT_DIR))
        self.limit_var = tk.IntVar(value=int(preferences.get("limit", 40)))
        self.download_limit_var = tk.IntVar(value=int(preferences.get("download_limit", 12)))
        self.min_edge_var = tk.IntVar(value=int(preferences.get("min_edge", DEFAULT_MIN_LONG_EDGE)))
        self.download_var = tk.BooleanVar(value=bool(preferences.get("download", False)))
        self.view_mode_var = tk.StringVar(value=str(preferences.get("view_mode") or "grid"))
        self.result_filter_var = tk.StringVar(value="")
        self.result_scope_var = tk.StringVar(value=RESULT_SCOPE_OPTIONS[0][0])
        self.result_count_var = tk.StringVar(value="0 项")
        self.gallery_page_var = tk.StringVar(value="第 1 / 1 页")
        self.library_var = tk.StringVar(value="本地资料库")

        self.phase_var = tk.StringVar(value="准备就绪")
        self.detail_var = tk.StringVar(value="准备就绪")
        self.found_var = tk.StringVar(value="收录 0")
        self.checked_var = tk.StringVar(value="检查 0")
        self.skipped_var = tk.StringVar(value="排除 0")
        self.downloaded_var = tk.StringVar(value="下载 0")
        self.issue_var = tk.StringVar(value="问题 0")
        self.elapsed_var = tk.StringVar(value="用时 0秒")

        self._configure_style()
        self._build_ui()
        if preferences.get("advanced_visible"):
            self._toggle_advanced()
        self._set_view_mode(self.view_mode_var.get())
        self.result_filter_var.trace_add("write", self._schedule_result_filter)
        self.result_scope_var.trace_add("write", self._schedule_result_filter)
        self.photographer_entry.bind("<Return>", self._submit_search_from_keyboard, add="+")
        self.bind("<Escape>", lambda _event: self._cancel_archive())
        self.bind("<F5>", lambda _event: self._load_existing_records())
        self.bind("<Control-f>", lambda _event: self.result_filter_entry.focus_set())
        self.bind("<Control-l>", lambda _event: self.photographer_entry.focus_set())
        self.bind("<Control-e>", lambda _event: self._edit_research_content())
        self.bind("<Prior>", lambda _event: self._change_gallery_page(-1))
        self.bind("<Next>", lambda _event: self._change_gallery_page(1))
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self._drain_queue)
        self.after(250, self._update_elapsed)
        if load_archive:
            self._load_existing_records()

    def report_callback_exception(self, exc_type, exc_value, exc_traceback) -> None:
        """Keep the application responsive and expose callback failures non-modally."""

        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback)).strip()
        self._add_issue()
        self._log(f"unexpected UI error: {detail}")
        self.phase_var.set("操作未完成")
        self.detail_var.set(f"{exc_value}；其他资料与当前任务已保留，详情见活动日志。")

    def _configure_style(self) -> None:
        self.configure(bg="#F2F4F5")
        style = ttk.Style(self)
        style.theme_use("clam")
        base_font = ("Microsoft YaHei UI", 10)
        small_font = ("Microsoft YaHei UI", 9)
        style.configure(".", font=base_font)
        style.configure("TFrame", background="#F2F4F5")
        style.configure("Surface.TFrame", background="#FFFFFF")
        style.configure("Inset.TFrame", background="#F6F8F9")
        style.configure("Header.TLabel", background="#F2F4F5", foreground="#152027", font=("Microsoft YaHei UI", 19, "bold"))
        style.configure("Subtle.TLabel", background="#F2F4F5", foreground="#65717B", font=small_font)
        style.configure("SurfaceTitle.TLabel", background="#FFFFFF", foreground="#152027", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("SurfaceText.TLabel", background="#FFFFFF", foreground="#29343C", font=base_font)
        style.configure("Muted.TLabel", background="#FFFFFF", foreground="#6D7881", font=small_font)
        style.configure("InsetMuted.TLabel", background="#F6F8F9", foreground="#6D7881", font=small_font)
        style.configure("Metric.TLabel", background="#F6F8F9", foreground="#34414A", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Phase.TLabel", background="#F6F8F9", foreground="#0E6B5B", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Counter.TLabel", background="#FFFFFF", foreground="#5F6C75", font=("Microsoft YaHei UI", 9))
        style.configure("TEntry", padding=(9, 7), fieldbackground="#FFFFFF", bordercolor="#C8D0D6", lightcolor="#C8D0D6", darkcolor="#C8D0D6")
        style.configure("TCombobox", padding=(9, 7), fieldbackground="#FFFFFF", bordercolor="#C8D0D6", arrowsize=15)
        style.configure("TButton", padding=(11, 7), background="#FFFFFF", foreground="#29343C", borderwidth=1, bordercolor="#C8D0D6")
        style.map("TButton", background=[("active", "#F5F7F8"), ("pressed", "#E9EEF0"), ("disabled", "#F2F4F5")])
        style.configure("Command.TButton", padding=(9, 6), background="#FFFFFF", foreground="#29343C", borderwidth=1, bordercolor="#D1D8DD")
        style.configure("Icon.TButton", padding=7, background="#FFFFFF", foreground="#29343C", borderwidth=1, bordercolor="#D1D8DD")
        style.configure("IconSelected.TButton", padding=7, background="#DCEFEA", foreground="#0D5C50", borderwidth=1, bordercolor="#93C8BC")
        style.map("Icon.TButton", background=[("active", "#F1F4F5")])
        style.map("IconSelected.TButton", background=[("active", "#CDE7E1")])
        style.configure("Disclosure.TButton", padding=(8, 6), background="#FFFFFF", foreground="#4B5963", borderwidth=0)
        style.configure("Accent.TButton", padding=(14, 8), background="#126B5C", foreground="#FFFFFF", borderwidth=0)
        style.map("Accent.TButton", background=[("active", "#0E584B"), ("disabled", "#A9CFC7")])
        style.configure("Stop.TButton", padding=(12, 8), background="#FFF7ED", foreground="#9A3412", borderwidth=1, bordercolor="#FDBA74")
        style.map("Stop.TButton", background=[("active", "#FFEDD5"), ("disabled", "#F3F4F6")], foreground=[("disabled", "#9CA3AF")])
        style.configure("Horizontal.TProgressbar", background="#126B5C", troughcolor="#DDE3EA", bordercolor="#DDE3EA")
        style.configure("Treeview", rowheight=64, background="#FFFFFF", fieldbackground="#FFFFFF", foreground="#1C272E", borderwidth=0)
        style.configure("Treeview.Heading", background="#EEF1F4", foreground="#4C5562", font=("Microsoft YaHei UI", 9, "bold"), relief="flat")
        style.map("Treeview", background=[("selected", "#D7EDE8")], foreground=[("selected", "#111827")])
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=(22, 16, 22, 14))
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        header = ttk.Frame(root)
        header.grid(row=0, column=0, sticky=tk.EW)
        header.columnconfigure(1, weight=1)
        if self.brand_image:
            ttk.Label(header, image=self.brand_image).grid(row=0, column=0, rowspan=2, sticky=tk.W, padx=(0, 10))
        ttk.Label(header, text=APP_TITLE, style="Header.TLabel").grid(row=0, column=1, sticky=tk.W)
        ttk.Label(header, text="PHOTOGRAPHIC RESEARCH ARCHIVE", style="Subtle.TLabel").grid(row=1, column=1, sticky=tk.W, pady=(2, 0))
        library_status = ttk.Frame(header)
        library_status.grid(row=0, column=2, rowspan=2, sticky=tk.E)
        database_icon = self.icons.get("database", "accent")
        if database_icon:
            ttk.Label(library_status, image=database_icon).grid(row=0, column=0, rowspan=2, padx=(0, 8))
        ttk.Label(library_status, textvariable=self.library_var, style="Subtle.TLabel").grid(row=0, column=1, sticky=tk.E)
        ttk.Label(library_status, text="LOCAL ARCHIVE", style="Subtle.TLabel").grid(row=1, column=1, sticky=tk.E)

        console = ttk.Frame(root, style="Surface.TFrame", padding=(14, 12))
        console.grid(row=1, column=0, sticky=tk.EW, pady=(14, 12))
        console.columnconfigure(1, weight=1)

        search_icon = self.icons.get("search", "dark")
        ttk.Label(console, image=search_icon, style="SurfaceText.TLabel").grid(row=0, column=0, sticky=tk.W, padx=(0, 10))
        self.photographer_entry = ttk.Combobox(console, textvariable=self.photographer_var, width=46)
        self.photographer_entry.grid(row=0, column=1, sticky=tk.EW)
        self.photographer_entry.bind("<<ComboboxSelected>>", lambda _event: self._on_history_selected())

        self.advanced_button = ttk.Button(
            console,
            text="检索设置",
            image=self.icons.get("sliders-horizontal", "dark"),
            compound=tk.LEFT,
            style="Disclosure.TButton",
            command=self._toggle_advanced,
        )
        self.advanced_button.grid(row=0, column=2, padx=(10, 6))
        self.start_button = ttk.Button(
            console,
            text="检索作品",
            image=self.icons.get("search", "light"),
            compound=tk.LEFT,
            style="Accent.TButton",
            command=self._start_archive,
        )
        self.start_button.grid(row=0, column=3, padx=(6, 0))
        self.cancel_button = ttk.Button(
            console,
            text="停止",
            image=self.icons.get("square-stop", "dark"),
            compound=tk.LEFT,
            style="Stop.TButton",
            command=self._cancel_archive,
            state=tk.DISABLED,
        )
        self.cancel_button.grid(row=0, column=4, padx=(8, 0))

        self.advanced_frame = ttk.Frame(console, style="Surface.TFrame")
        self.advanced_frame.grid(row=1, column=0, columnspan=5, sticky=tk.EW, pady=(12, 0))
        self.advanced_frame.columnconfigure(1, weight=1)
        self.advanced_frame.columnconfigure(3, weight=1)
        ttk.Label(self.advanced_frame, text="官网 URL", style="Muted.TLabel").grid(row=0, column=0, sticky=tk.W, padx=(0, 8))
        ttk.Entry(self.advanced_frame, textvariable=self.website_url_var).grid(row=0, column=1, columnspan=3, sticky=tk.EW)
        ttk.Label(self.advanced_frame, text="归档目录", style="Muted.TLabel").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=(10, 0))
        ttk.Entry(self.advanced_frame, textvariable=self.output_dir_var).grid(row=1, column=1, columnspan=2, sticky=tk.EW, pady=(10, 0))
        choose_button = self._make_icon_button(self.advanced_frame, "folder-open", "选择归档目录", self._choose_output_dir)
        choose_button.grid(row=1, column=3, sticky=tk.E, padx=(8, 0), pady=(10, 0))
        advanced_settings = ttk.Frame(self.advanced_frame, style="Surface.TFrame")
        advanced_settings.grid(row=2, column=0, columnspan=4, sticky=tk.EW, pady=(10, 0))
        ttk.Label(advanced_settings, text="收录", style="Muted.TLabel").grid(row=0, column=0)
        ttk.Spinbox(advanced_settings, from_=1, to=5000, textvariable=self.limit_var, width=6).grid(row=0, column=1, padx=(6, 16))
        ttk.Label(advanced_settings, text="下载", style="Muted.TLabel").grid(row=0, column=2)
        ttk.Spinbox(advanced_settings, from_=0, to=5000, textvariable=self.download_limit_var, width=6).grid(row=0, column=3, padx=(6, 16))
        ttk.Label(advanced_settings, text="最短长边", style="Muted.TLabel").grid(row=0, column=4)
        ttk.Spinbox(advanced_settings, from_=0, to=10000, increment=100, textvariable=self.min_edge_var, width=7).grid(row=0, column=5, padx=(6, 4))
        ttk.Label(advanced_settings, text="px", style="Muted.TLabel").grid(row=0, column=6, padx=(0, 16))
        ttk.Checkbutton(advanced_settings, text="检索后自动下载", variable=self.download_var).grid(row=0, column=7)
        self.advanced_frame.grid_remove()

        progress_area = ttk.Frame(console, style="Inset.TFrame", padding=(12, 9))
        progress_area.grid(row=2, column=0, columnspan=5, sticky=tk.EW, pady=(10, 0))
        progress_area.columnconfigure(1, weight=1)
        ttk.Label(progress_area, textvariable=self.phase_var, style="Phase.TLabel").grid(row=0, column=0, sticky=tk.W)
        ttk.Label(progress_area, textvariable=self.detail_var, style="InsetMuted.TLabel", wraplength=820).grid(row=0, column=1, sticky=tk.W, padx=(12, 0))
        self.progress = ttk.Progressbar(progress_area, mode="determinate", maximum=100)
        self.progress.grid(row=1, column=0, columnspan=2, sticky=tk.EW, pady=(10, 0))
        metrics = ttk.Frame(progress_area, style="Inset.TFrame")
        metrics.grid(row=2, column=0, columnspan=2, sticky=tk.EW, pady=(10, 0))
        for index, variable in enumerate((self.found_var, self.checked_var, self.skipped_var, self.downloaded_var, self.issue_var, self.elapsed_var)):
            ttk.Label(metrics, textvariable=variable, style="Metric.TLabel").grid(row=0, column=index, sticky=tk.W, padx=(0, 16))

        work = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        self.work_pane = work
        work.grid(row=2, column=0, sticky=tk.NSEW)

        left = ttk.Frame(work, style="Surface.TFrame", padding=(12, 10))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(left, style="Surface.TFrame")
        toolbar.grid(row=0, column=0, sticky=tk.EW)
        toolbar.columnconfigure(2, weight=1)
        ttk.Label(toolbar, text="作品流", style="SurfaceTitle.TLabel").grid(row=0, column=0, sticky=tk.W)
        ttk.Label(toolbar, textvariable=self.result_count_var, style="Counter.TLabel").grid(row=0, column=1, sticky=tk.W, padx=(8, 12))
        self.grid_view_button = self._make_icon_button(toolbar, "grid-2x2", "画廊视图", lambda: self._set_view_mode("grid"), style="IconSelected.TButton")
        self.grid_view_button.grid(row=0, column=3, padx=(0, 4))
        self.list_view_button = self._make_icon_button(toolbar, "list", "列表视图", lambda: self._set_view_mode("list"))
        self.list_view_button.grid(row=0, column=4, padx=(0, 8))
        self.download_selected_button = self._make_icon_button(toolbar, "download", "下载选中原图", self._download_selected_record)
        self.download_selected_button.grid(row=0, column=5, padx=(0, 4))
        self.download_list_button = self._make_icon_button(toolbar, "hard-drive-download", "下载当前结果", self._download_current_list)
        self.download_list_button.grid(row=0, column=6, padx=(0, 10))
        self.compare_button = self._make_icon_button(toolbar, "columns-2", "将选中作品加入对比", self._toggle_selected_comparison)
        self.compare_button.grid(row=0, column=7, padx=(0, 4))
        self.contact_sheet_button = self._make_icon_button(toolbar, "layout-grid", "导出当前筛选联系表", self._export_contact_sheet)
        self.contact_sheet_button.grid(row=0, column=8, padx=(0, 10))
        self.refresh_button = self._make_icon_button(toolbar, "refresh-cw", "刷新本地资料库", self._load_existing_records)
        self.refresh_button.grid(row=0, column=9, padx=(0, 4))
        self.clear_cache_button = self._make_icon_button(toolbar, "trash-2", "清空当前摄影师缓存", self._clear_current_cache)
        self.clear_cache_button.grid(row=0, column=10)

        filter_frame = ttk.Frame(toolbar, style="Surface.TFrame")
        filter_frame.grid(row=1, column=0, columnspan=7, sticky=tk.W, pady=(9, 0))
        ttk.Label(filter_frame, image=self.icons.get("funnel", "dark"), style="SurfaceText.TLabel").grid(row=0, column=0, padx=(0, 6))
        ttk.Label(filter_frame, text="筛选", style="Muted.TLabel").grid(row=0, column=1, padx=(0, 6))
        self.result_filter_entry = ttk.Entry(filter_frame, textvariable=self.result_filter_var, width=22)
        self.result_filter_entry.grid(row=0, column=2, padx=(0, 8))
        self.result_scope_combo = ttk.Combobox(
            filter_frame,
            textvariable=self.result_scope_var,
            values=[label for label, _scope in RESULT_SCOPE_OPTIONS],
            width=12,
            state="readonly",
        )
        self.result_scope_combo.grid(row=0, column=3)

        self.gallery_pager = ttk.Frame(toolbar, style="Surface.TFrame")
        self.gallery_pager.grid(row=1, column=7, columnspan=4, sticky=tk.E, pady=(9, 0))
        self.page_previous_button = self._make_icon_button(
            self.gallery_pager,
            "chevron-left",
            "上一页",
            lambda: self._change_gallery_page(-1),
        )
        self.page_previous_button.grid(row=0, column=0, padx=(0, 5))
        ttk.Label(self.gallery_pager, textvariable=self.gallery_page_var, style="Counter.TLabel").grid(row=0, column=1, padx=4)
        self.page_next_button = self._make_icon_button(
            self.gallery_pager,
            "chevron-right",
            "下一页",
            lambda: self._change_gallery_page(1),
        )
        self.page_next_button.grid(row=0, column=2, padx=(5, 0))

        results_stack = ttk.Frame(left, style="Surface.TFrame")
        results_stack.grid(row=1, column=0, sticky=tk.NSEW, pady=(10, 0))
        results_stack.columnconfigure(0, weight=1)
        results_stack.rowconfigure(0, weight=1)

        self.gallery_frame = tk.Frame(results_stack, bg="#F6F8F9")
        self.gallery_frame.grid(row=0, column=0, sticky=tk.NSEW)
        self.gallery_frame.grid_rowconfigure(0, weight=1)
        self.gallery_frame.grid_columnconfigure(0, weight=1)
        self.gallery_canvas = tk.Canvas(self.gallery_frame, bg="#F6F8F9", highlightthickness=0, borderwidth=0)
        self.gallery_scroll = ttk.Scrollbar(self.gallery_frame, orient=tk.VERTICAL, command=self.gallery_canvas.yview)
        self.gallery_canvas.configure(yscrollcommand=self.gallery_scroll.set)
        self.gallery_canvas.grid(row=0, column=0, sticky=tk.NSEW)
        self.gallery_scroll.grid(row=0, column=1, sticky=tk.NS)
        self.gallery_inner = tk.Frame(self.gallery_canvas, bg="#F6F8F9")
        self.gallery_window = self.gallery_canvas.create_window((0, 0), window=self.gallery_inner, anchor=tk.NW)
        self.gallery_empty = tk.Label(
            self.gallery_canvas,
            text="当前摄影师尚无已验证作品",
            bg="#F6F8F9",
            fg="#6D7881",
            font=("Microsoft YaHei UI", 11),
        )
        self.gallery_empty.place(relx=0.5, rely=0.42, anchor=tk.CENTER)
        self.gallery_canvas.bind("<Configure>", self._on_gallery_resize)
        self.gallery_inner.bind("<Configure>", self._update_gallery_scrollregion)
        self.gallery_canvas.bind("<MouseWheel>", self._on_gallery_mousewheel)
        self.gallery_canvas.bind("<Left>", lambda _event: self._move_gallery_selection(-1))
        self.gallery_canvas.bind("<Right>", lambda _event: self._move_gallery_selection(1))

        self.table_frame = ttk.Frame(results_stack, style="Surface.TFrame")
        self.table_frame.grid(row=0, column=0, sticky=tk.NSEW)
        self.table_frame.columnconfigure(0, weight=1)
        self.table_frame.rowconfigure(0, weight=1)

        columns = ("status", "match", "resolution", "license", "source")
        self.record_tree = ttk.Treeview(self.table_frame, columns=columns, show="tree headings", selectmode="browse")
        self.record_tree.heading("#0", text="作品 / 文件")
        self.record_tree.heading("status", text="状态")
        self.record_tree.heading("match", text="匹配")
        self.record_tree.heading("resolution", text="尺寸")
        self.record_tree.heading("license", text="系列 / 项目")
        self.record_tree.heading("source", text="来源")
        self.record_tree.column("#0", width=330, minwidth=210)
        self.record_tree.column("status", width=86, anchor=tk.CENTER)
        self.record_tree.column("match", width=74, anchor=tk.CENTER)
        self.record_tree.column("resolution", width=96, anchor=tk.CENTER)
        self.record_tree.column("license", width=136)
        self.record_tree.column("source", width=108)
        self.record_tree.grid(row=0, column=0, sticky=tk.NSEW)
        tree_scroll = ttk.Scrollbar(self.table_frame, orient=tk.VERTICAL, command=self._scroll_record_tree)
        self.record_tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.grid(row=0, column=1, sticky=tk.NS)
        self.record_tree.bind("<<TreeviewSelect>>", lambda _event: self._show_selected_record())
        self.record_tree.bind("<Double-Button-1>", lambda _event: self._open_fullscreen_viewer())
        self.record_tree.bind("<Button-3>", self._show_record_context_menu)
        self.record_tree.bind("<MouseWheel>", lambda _event: self.after_idle(self._load_visible_tree_thumbnails), add="+")
        self.record_tree.bind("<Configure>", lambda _event: self.after_idle(self._load_visible_tree_thumbnails), add="+")
        self.record_tree.tag_configure("downloaded", foreground="#126B5C")
        self.record_tree.tag_configure("duplicate", foreground="#9A3412")
        self.record_tree.tag_configure("pending", foreground="#252A31")
        self.record_tree.tag_configure("skipped", foreground="#7A828C")
        self.table_frame.grid_remove()
        self.record_context_menu = tk.Menu(
            self,
            tearoff=False,
            bg="#FFFFFF",
            fg="#29343C",
            activebackground="#DCEFEA",
            activeforeground="#0D5C50",
            borderwidth=1,
        )
        self.record_context_menu.add_command(
            label="下载原图",
            image=self.icons.get("download", "dark"),
            compound=tk.LEFT,
            command=self._download_selected_record,
        )
        self.record_context_menu.add_command(
            label="全屏查看",
            image=self.icons.get("maximize", "dark"),
            compound=tk.LEFT,
            command=self._open_fullscreen_viewer,
        )
        self.record_context_menu.add_command(
            label="加入 / 移出对比",
            image=self.icons.get("columns-2", "dark"),
            compound=tk.LEFT,
            command=self._toggle_selected_comparison,
        )
        self.record_context_menu.add_command(
            label="打开双图对比",
            image=self.icons.get("scan", "dark"),
            compound=tk.LEFT,
            command=self._open_compare_window,
        )
        self.record_context_menu.add_command(
            label="编辑研究内容",
            image=self.icons.get("settings-2", "dark"),
            compound=tk.LEFT,
            command=self._edit_research_content,
        )
        self.record_context_menu.add_command(
            label="导出研究档案",
            image=self.icons.get("hard-drive-download", "dark"),
            compound=tk.LEFT,
            command=self._export_research_record,
        )
        self.record_context_menu.add_separator()
        self.record_context_menu.add_command(
            label="打开来源页面",
            image=self.icons.get("external-link", "dark"),
            compound=tk.LEFT,
            command=self._open_source_page,
        )
        self.record_context_menu.add_command(
            label="打开本地文件",
            image=self.icons.get("file-image", "dark"),
            compound=tk.LEFT,
            command=self._open_selected_file,
        )
        self.record_context_menu.add_separator()
        self.record_context_menu.add_command(
            label="打开归档目录",
            image=self.icons.get("folder-open", "dark"),
            compound=tk.LEFT,
            command=self._open_output_dir,
        )
        work.add(left, weight=7)

        right = ttk.Frame(work, style="Surface.TFrame", padding=(12, 10))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=4, minsize=250)
        right.rowconfigure(3, weight=2)
        preview_bar = ttk.Frame(right, style="Surface.TFrame")
        preview_bar.grid(row=0, column=0, sticky=tk.EW)
        preview_bar.columnconfigure(0, weight=1)
        ttk.Label(preview_bar, text="作品检查器", style="SurfaceTitle.TLabel").grid(row=0, column=0, sticky=tk.W)
        self._make_icon_button(preview_bar, "maximize", "全屏查看", self._open_fullscreen_viewer).grid(row=0, column=1, padx=(4, 0))
        self._make_icon_button(preview_bar, "columns-2", "加入 / 移出对比", self._toggle_selected_comparison).grid(row=0, column=2, padx=(4, 0))
        self._make_icon_button(preview_bar, "settings-2", "编辑研究内容", self._edit_research_content).grid(row=0, column=3, padx=(4, 0))
        self._make_icon_button(preview_bar, "external-link", "打开来源页面", self._open_source_page).grid(row=0, column=4, padx=(4, 0))
        self.preview_more_menu = tk.Menu(
            self,
            tearoff=False,
            bg="#FFFFFF",
            fg="#29343C",
            activebackground="#DCEFEA",
            activeforeground="#0D5C50",
            borderwidth=1,
        )
        self.preview_more_menu.add_command(
            label="导出研究档案",
            image=self.icons.get("hard-drive-download", "dark"),
            compound=tk.LEFT,
            command=self._export_research_record,
        )
        self.preview_more_menu.add_command(
            label="打开本地文件",
            image=self.icons.get("file-image", "dark"),
            compound=tk.LEFT,
            command=self._open_selected_file,
        )
        self.preview_more_menu.add_separator()
        self.preview_more_menu.add_command(
            label="打开归档目录",
            image=self.icons.get("folder-open", "dark"),
            compound=tk.LEFT,
            command=self._open_output_dir,
        )
        self.preview_more_button = self._make_icon_button(
            preview_bar,
            "chevron-down",
            "更多作品操作",
            self._show_preview_more_menu,
        )
        self.preview_more_button.grid(row=0, column=5, padx=(4, 0))
        preview_surface = tk.Frame(right, bg="#20272E", highlightthickness=0)
        preview_surface.grid(row=1, column=0, sticky=tk.NSEW, pady=(10, 10))
        self.preview_label = tk.Label(
            preview_surface,
            text="暂无选中作品",
            anchor=tk.CENTER,
            bg="#20272E",
            fg="#AEB8BF",
            font=("Microsoft YaHei UI", 10),
        )
        self.preview_label.pack(fill=tk.BOTH, expand=True)
        ttk.Label(right, text="作品资料与研究笔记", style="SurfaceTitle.TLabel").grid(row=2, column=0, sticky=tk.W)
        self.detail_text = ScrolledText(right, height=8, wrap=tk.WORD, borderwidth=0, font=("Microsoft YaHei UI", 10))
        self.detail_text.grid(row=3, column=0, sticky=tk.NSEW, pady=(10, 0))
        self.detail_text.configure(bg="#F6F8F9", fg="#29343C", padx=12, pady=10, relief=tk.FLAT)
        work.add(right, weight=6)
        self.after(160, self._set_default_sash)

        footer = ttk.Frame(root, style="Surface.TFrame", padding=(10, 6))
        footer.grid(row=3, column=0, sticky=tk.EW, pady=(10, 0))
        footer.columnconfigure(1, weight=1)
        self.log_toggle_button = ttk.Button(
            footer,
            text="活动日志",
            image=self.icons.get("logs", "dark"),
            compound=tk.LEFT,
            style="Disclosure.TButton",
            command=self._toggle_log,
        )
        self.log_toggle_button.grid(row=0, column=0, sticky=tk.W)

        self.log_panel = ttk.Frame(root, style="Surface.TFrame", padding=(10, 8))
        self.log_panel.grid(row=4, column=0, sticky=tk.EW)
        self.log_panel.columnconfigure(0, weight=1)
        self.log_text = tk.Text(self.log_panel, height=5, wrap=tk.WORD, borderwidth=0, font=("Cascadia Mono", 9))
        self.log_text.grid(row=0, column=0, sticky=tk.EW)
        self.log_text.configure(bg="#F6F8F9", fg="#53616B", padx=10, pady=8, relief=tk.FLAT)
        self.log_panel.grid_remove()

    def _make_icon_button(
        self,
        parent: tk.Misc,
        icon_name: str,
        tooltip: str,
        command,
        style: str = "Icon.TButton",
    ) -> ttk.Button:
        icon = self.icons.get(icon_name, "dark")
        button = ttk.Button(parent, image=icon, command=command, style=style, width=3)
        if icon is None:
            button.configure(text=tooltip[:2], width=4)
        ToolTip(button, tooltip)
        return button

    def _show_preview_more_menu(self) -> None:
        button = self.preview_more_button
        try:
            self.preview_more_menu.update_idletasks()
            x = max(
                0,
                button.winfo_rootx() + button.winfo_width() - self.preview_more_menu.winfo_reqwidth(),
            )
            self.preview_more_menu.tk_popup(
                x,
                button.winfo_rooty() + button.winfo_height() + 2,
            )
        finally:
            self.preview_more_menu.grab_release()

    def _set_default_sash(self) -> None:
        try:
            width = self.work_pane.winfo_width()
            if width > 800:
                self.work_pane.sashpos(0, int(width * 0.62))
        except tk.TclError:
            return

    def _submit_search_from_keyboard(self, _event: tk.Event) -> str:
        self._start_archive()
        return "break"

    def _preferences_payload(self) -> dict[str, object]:
        try:
            limit = int(self.limit_var.get())
        except (tk.TclError, TypeError, ValueError):
            limit = 40
        try:
            download_limit = int(self.download_limit_var.get())
        except (tk.TclError, TypeError, ValueError):
            download_limit = 12
        try:
            min_edge = int(self.min_edge_var.get())
        except (tk.TclError, TypeError, ValueError):
            min_edge = DEFAULT_MIN_LONG_EDGE
        website_url = self.website_url_var.get().strip()
        return {
            "version": PREFERENCES_VERSION,
            "photographer": self.photographer_var.get().strip(),
            "website_url": website_url,
            "website_is_auto": bool(
                website_url and website_url == self.auto_website_url and self.photographer_var.get().strip() == self.auto_website_name
            ),
            "output_dir": self.output_dir_var.get().strip(),
            "limit": _bounded_int(limit, 40, 1, 5000),
            "download_limit": _bounded_int(download_limit, 12, 0, 5000),
            "min_edge": _bounded_int(min_edge, DEFAULT_MIN_LONG_EDGE, 0, 10000),
            "download": bool(self.download_var.get()),
            "view_mode": self.view_mode_var.get() if self.view_mode_var.get() in {"grid", "list"} else "grid",
            "advanced_visible": self.advanced_visible,
        }

    def _save_preferences(self) -> None:
        try:
            _write_preferences(self.preferences_path, self._preferences_payload())
        except OSError as exc:
            self._add_issue()
            self._log(f"preferences save error: {exc}")

    def _toggle_advanced(self) -> None:
        self.advanced_visible = not self.advanced_visible
        if self.advanced_visible:
            self.advanced_frame.grid()
            self.advanced_button.configure(text="收起设置")
        else:
            self.advanced_frame.grid_remove()
            self.advanced_button.configure(text="检索设置")

    def _toggle_log(self) -> None:
        self.log_visible = not self.log_visible
        if self.log_visible:
            if self.log_flush_after is not None:
                self.after_cancel(self.log_flush_after)
                self.log_flush_after = None
            self._flush_log_lines()
            self.log_panel.grid()
            self.log_toggle_button.configure(text="收起日志")
            self.log_text.see(tk.END)
        else:
            self.log_panel.grid_remove()
            self.log_toggle_button.configure(text="活动日志")

    def _on_history_selected(self) -> None:
        if self.busy:
            return
        self._load_existing_records()

    def _refresh_history(self) -> None:
        db_path = Path(self.output_dir_var.get()).expanduser() / "photo_archive.db"
        if not db_path.exists():
            self.photographer_entry.configure(values=())
            self.library_var.set("本地资料库")
            return
        history = ArchiveStore(db_path).list_search_names()
        self.photographer_entry.configure(values=[name for name, _count in history])
        total = sum(count for _name, count in history)
        self.library_var.set(f"{len(history)} 位摄影师 · {total} 条记录")

    def _set_view_mode(self, mode: str) -> None:
        if mode not in {"grid", "list"}:
            return
        self.view_mode_var.set(mode)
        if mode == "grid":
            self.table_frame.grid_remove()
            self.gallery_frame.grid()
            self.gallery_pager.grid()
            self.grid_view_button.configure(style="IconSelected.TButton")
            self.list_view_button.configure(style="Icon.TButton")
            self._schedule_gallery_reflow()
            self._set_gallery_empty_state()
        else:
            self.gallery_frame.grid_remove()
            self.table_frame.grid()
            self.gallery_pager.grid_remove()
            self.grid_view_button.configure(style="Icon.TButton")
            self.list_view_button.configure(style="IconSelected.TButton")
            self.after_idle(self._load_visible_tree_thumbnails)

    def _schedule_result_filter(self, *_args: object) -> None:
        self.gallery_page_index = 0
        if self.filter_after:
            self.after_cancel(self.filter_after)
        self.filter_after = self.after(140, self._apply_result_filter)

    def _apply_result_filter(self) -> None:
        self.filter_after = None
        for source_key in self.gallery_order:
            iid = self.record_iids.get(source_key)
            record = self.records.get(iid or "")
            if not iid or record is None:
                continue
            self._sync_tree_record_visibility(iid, record)
        visible_keys = self._visible_source_keys()
        if self.selected_source_key not in visible_keys and visible_keys:
            self._select_gallery_record(visible_keys[0])
        elif self.records and not visible_keys:
            self._clear_record_selection("当前筛选无结果")
        self._schedule_gallery_reflow()
        self._update_result_count(visible_keys)
        self._set_gallery_empty_state(visible_keys)
        if self.view_mode_var.get() == "list":
            self.after_idle(self._load_visible_tree_thumbnails)

    def _record_matches_filter(self, record: PhotoRecord) -> bool:
        scope = RESULT_SCOPE_CODES.get(self.result_scope_var.get(), "all")
        if not _record_matches_scope(record, scope):
            return False
        query = self.result_filter_var.get().strip().casefold()
        if not query:
            return True
        haystack = " ".join(
            (
                record.title,
                record.collection_title,
                record.author,
                record.annotation,
                record.source_comment,
                record.research_note,
                record.research_tags,
                str(record.rating or ""),
                record.shooting_date,
                record.camera_make,
                record.camera_model,
                record.match_reason,
            )
        ).casefold()
        return all(token in haystack for token in query.split())

    def _sync_tree_record_visibility(self, iid: str, record: PhotoRecord) -> None:
        if self._record_matches_filter(record):
            self.record_tree.reattach(iid, "", tk.END)
        else:
            self.record_tree.detach(iid)

    def _scroll_record_tree(self, *args: object) -> None:
        self.record_tree.yview(*args)
        self.after_idle(self._load_visible_tree_thumbnails)

    def _load_visible_tree_thumbnails(self) -> None:
        if self.view_mode_var.get() != "list" or not self.record_tree.winfo_exists():
            return
        height = max(1, self.record_tree.winfo_height())
        visible_iids = {
            iid
            for y in range(0, height + 24, 24)
            if (iid := self.record_tree.identify_row(y))
        }
        if not visible_iids:
            visible_iids = set(self.record_tree.get_children("")[:GALLERY_PAGE_SIZE])
        for iid in visible_iids:
            record = self.records.get(iid)
            if record:
                self._start_thumbnail_load(record)

    def _on_gallery_resize(self, event: tk.Event) -> None:
        self.gallery_canvas.itemconfigure(self.gallery_window, width=max(1, event.width))
        self._schedule_gallery_reflow()

    def _update_gallery_scrollregion(self, _event: object = None) -> None:
        self.gallery_canvas.configure(scrollregion=self.gallery_canvas.bbox("all"))

    def _schedule_gallery_reflow(self) -> None:
        if self.gallery_reflow_after is not None:
            return
        self.gallery_reflow_after = self.after(GALLERY_REFLOW_DELAY_MS, self._reflow_gallery)

    def _schedule_result_summary_refresh(self) -> None:
        if self.result_summary_after is not None:
            return
        self.result_summary_after = self.after(RESULT_SUMMARY_DELAY_MS, self._refresh_result_summary)

    def _refresh_result_summary(self) -> None:
        self.result_summary_after = None
        visible_keys = self._visible_source_keys()
        self._update_result_count(visible_keys)
        self._set_gallery_empty_state(visible_keys)

    def _reflow_gallery(self) -> None:
        """Lay out only visible cards after resize/filter bursts settle.

        Card dimensions remain fixed so live titles and thumbnails cannot shift the
        surrounding layout. @codex-comment approved
        """

        self.gallery_reflow_after = None
        width = max(GALLERY_CARD_WIDTH + GALLERY_GAP * 2, self.gallery_canvas.winfo_width())
        columns = max(1, (width - GALLERY_GAP) // (GALLERY_CARD_WIDTH + GALLERY_GAP))
        visible_keys = self._visible_source_keys()
        page_keys, self.gallery_page_index, page_count = _gallery_page_window(visible_keys, self.gallery_page_index)
        self._reconcile_gallery_page(page_keys)
        self._update_gallery_pager(len(visible_keys), page_count)
        for payload in self.gallery_cards.values():
            payload["frame"].grid_forget()
        occupied = columns * GALLERY_CARD_WIDTH + max(0, columns - 1) * GALLERY_GAP
        left_pad = max(GALLERY_GAP, (width - occupied) // 2)
        for index, source_key in enumerate(page_keys):
            payload = self.gallery_cards.get(source_key)
            if not payload:
                continue
            row, column = divmod(index, columns)
            padx = (left_pad if column == 0 else GALLERY_GAP, 0)
            payload["frame"].grid(row=row, column=column, padx=padx, pady=(GALLERY_GAP, 0), sticky=tk.NW)
        self.gallery_inner.update_idletasks()
        self._update_gallery_scrollregion()

    def _reconcile_gallery_page(self, page_keys: list[str]) -> None:
        desired = set(page_keys)
        for source_key in list(self.gallery_cards):
            if source_key not in desired:
                self._destroy_gallery_card(source_key)
        for source_key in page_keys:
            iid = self.record_iids.get(source_key)
            record = self.records.get(iid or "")
            if record:
                self._ensure_gallery_card(record, schedule_reflow=False)
                self._start_thumbnail_load(record)
        self._update_gallery_selection()

    def _update_gallery_pager(self, visible_count: int, page_count: int | None = None) -> None:
        if page_count is None:
            _page_keys, self.gallery_page_index, page_count = _gallery_page_window(
                self._visible_source_keys(),
                self.gallery_page_index,
            )
        self.gallery_page_var.set(f"第 {self.gallery_page_index + 1} / {page_count} 页")
        self.page_previous_button.configure(state=tk.NORMAL if visible_count and self.gallery_page_index > 0 else tk.DISABLED)
        self.page_next_button.configure(
            state=tk.NORMAL if visible_count and self.gallery_page_index < page_count - 1 else tk.DISABLED
        )

    def _change_gallery_page(self, delta: int) -> str:
        if self.view_mode_var.get() != "grid":
            return "break"
        visible_keys = self._visible_source_keys()
        _page_keys, current_page, page_count = _gallery_page_window(visible_keys, self.gallery_page_index)
        target_page = max(0, min(page_count - 1, current_page + int(delta)))
        if target_page == current_page:
            return "break"
        self.gallery_page_index = target_page
        page_keys, _page_index, _page_count = _gallery_page_window(visible_keys, target_page)
        self.gallery_canvas.yview_moveto(0)
        self._schedule_gallery_reflow()
        if page_keys:
            self._select_gallery_record(page_keys[0])
        return "break"

    def _visible_source_keys(self) -> list[str]:
        visible: list[str] = []
        for source_key in self.gallery_order:
            iid = self.record_iids.get(source_key)
            record = self.records.get(iid or "")
            if record and self._record_matches_filter(record):
                visible.append(source_key)
        return visible

    def _on_gallery_mousewheel(self, event: tk.Event) -> str:
        self.gallery_canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _ensure_gallery_card(self, record: PhotoRecord, *, schedule_reflow: bool = True) -> None:
        payload = self.gallery_cards.get(record.source_key)
        if payload:
            payload["title"].configure(text=self._compact_title(record.title))
            payload["meta"].configure(text=self._gallery_meta(record))
            return
        card = tk.Frame(
            self.gallery_inner,
            width=GALLERY_CARD_WIDTH,
            height=GALLERY_CARD_HEIGHT,
            bg="#FFFFFF",
            highlightbackground="#D8DEE2",
            highlightcolor="#D8DEE2",
            highlightthickness=1,
            cursor="hand2",
        )
        card.grid_propagate(False)
        image_label = tk.Label(card, text="正在载入预览", bg="#E9EEF0", fg="#7A858D", anchor=tk.CENTER, cursor="hand2")
        image_label.place(x=8, y=8, width=GALLERY_CARD_WIDTH - 16, height=148)
        cached_image = self.thumbnail_images.get(record.source_key)
        if cached_image is not None:
            image_label.configure(image=cached_image, text="", bg="#20272E")
        title_label = tk.Label(
            card,
            text=self._compact_title(record.title),
            bg="#FFFFFF",
            fg="#1C272E",
            anchor=tk.NW,
            justify=tk.LEFT,
            wraplength=GALLERY_CARD_WIDTH - 22,
            font=("Microsoft YaHei UI", 9, "bold"),
            cursor="hand2",
        )
        title_label.place(x=10, y=164, width=GALLERY_CARD_WIDTH - 20, height=32)
        meta_label = tk.Label(
            card,
            text=self._gallery_meta(record),
            bg="#FFFFFF",
            fg="#6B7680",
            anchor=tk.W,
            font=("Microsoft YaHei UI", 8),
            cursor="hand2",
        )
        meta_label.place(x=10, y=198, width=GALLERY_CARD_WIDTH - 20, height=16)
        compare_label = tk.Label(
            card,
            text="",
            bg="#126B5C",
            fg="#FFFFFF",
            font=("Microsoft YaHei UI", 8, "bold"),
            anchor=tk.CENTER,
            cursor="hand2",
        )
        payload = {"frame": card, "image": image_label, "title": title_label, "meta": meta_label, "compare": compare_label}
        self.gallery_cards[record.source_key] = payload
        for widget in payload.values():
            widget.bind("<Button-1>", lambda _event, key=record.source_key: self._select_gallery_record(key))
            widget.bind("<Double-Button-1>", lambda _event, key=record.source_key: self._open_gallery_record(key))
            widget.bind("<Button-3>", lambda event, key=record.source_key: self._show_record_context_menu(event, key))
            widget.bind("<MouseWheel>", self._on_gallery_mousewheel)
        if schedule_reflow:
            self._schedule_gallery_reflow()

    @staticmethod
    def _compact_title(value: str, limit: int = 50) -> str:
        text = " ".join((value or "Untitled").split())
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    def _gallery_meta(self, record: PhotoRecord) -> str:
        resolution = record.resolution or "尺寸待确认"
        source = self._compact_title(record.collection_title, 14) if record.collection_title else (
            "官网" if record.source == "website" else "开放图库"
        )
        rating = f"  ·  {record.rating}/5" if record.rating else ""
        return f"{resolution}  ·  {source}{rating}"

    def _destroy_gallery_card(self, source_key: str) -> None:
        payload = self.gallery_cards.pop(source_key, None)
        if payload:
            payload["frame"].destroy()

    def _open_gallery_record(self, source_key: str) -> str:
        self._select_gallery_record(source_key)
        self._open_fullscreen_viewer()
        return "break"

    def _select_gallery_record(self, source_key: str) -> None:
        iid = self.record_iids.get(source_key)
        if not iid:
            return
        self.record_tree.selection_set(iid)
        self.record_tree.focus(iid)
        self.selected_source_key = source_key
        self._update_gallery_selection()
        self._show_selected_record()
        if self.view_mode_var.get() == "grid":
            self.gallery_canvas.focus_set()
        else:
            self.record_tree.focus_set()
        self.after_idle(lambda key=source_key: self._ensure_gallery_card_visible(key))

    def _show_record_context_menu(self, event: tk.Event, source_key: str = "") -> str:
        if source_key:
            self._select_gallery_record(source_key)
        elif isinstance(event.widget, ttk.Treeview):
            iid = event.widget.identify_row(event.y)
            if not iid:
                return "break"
            event.widget.selection_set(iid)
            event.widget.focus(iid)
            self._show_selected_record()
        self.record_context_menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _ensure_gallery_card_visible(self, source_key: str) -> None:
        payload = self.gallery_cards.get(source_key)
        if not payload:
            return
        frame = payload["frame"]
        inner_height = max(1, self.gallery_inner.winfo_height())
        top = self.gallery_canvas.canvasy(0)
        bottom = top + self.gallery_canvas.winfo_height()
        card_top = frame.winfo_y()
        card_bottom = card_top + frame.winfo_height()
        if card_top < top:
            self.gallery_canvas.yview_moveto(max(0.0, card_top / inner_height))
        elif card_bottom > bottom:
            target = max(0.0, (card_bottom - self.gallery_canvas.winfo_height()) / inner_height)
            self.gallery_canvas.yview_moveto(min(1.0, target))

    def _update_gallery_selection(self) -> None:
        for source_key, payload in self.gallery_cards.items():
            selected = source_key == self.selected_source_key
            payload["frame"].configure(
                highlightbackground="#126B5C" if selected else "#D8DEE2",
                highlightcolor="#126B5C" if selected else "#D8DEE2",
                highlightthickness=2 if selected else 1,
            )
            compare_label = payload.get("compare")
            if compare_label is not None:
                if source_key in self.compare_source_keys:
                    slot = self.compare_source_keys.index(source_key)
                    compare_label.configure(text="A" if slot == 0 else "B")
                    compare_label.place(x=210, y=16, width=26, height=22)
                    compare_label.lift()
                else:
                    compare_label.place_forget()

    def _move_gallery_selection(self, delta: int) -> str:
        visible = self._visible_source_keys()
        if not visible:
            return "break"
        try:
            current = visible.index(self.selected_source_key)
        except ValueError:
            current = -1 if delta > 0 else 0
        target_index = (current + delta) % len(visible)
        target_page = target_index // GALLERY_PAGE_SIZE
        if target_page != self.gallery_page_index:
            self.gallery_page_index = target_page
            self.gallery_canvas.yview_moveto(0)
            self._schedule_gallery_reflow()
        target = visible[target_index]
        self._select_gallery_record(target)
        return "break"

    def _update_result_count(self, visible_keys: list[str] | None = None) -> None:
        total = len(self.records)
        visible_keys = self._visible_source_keys() if visible_keys is None else visible_keys
        visible = len(visible_keys)
        self.result_count_var.set(f"{visible}/{total} 项" if visible != total else f"{total} 项")
        _page_keys, self.gallery_page_index, page_count = _gallery_page_window(visible_keys, self.gallery_page_index)
        self._update_gallery_pager(visible, page_count)

    def _set_gallery_empty_state(self, visible_keys: list[str] | None = None) -> None:
        if self.view_mode_var.get() != "grid":
            self.gallery_empty.place_forget()
            return
        visible_keys = self._visible_source_keys() if visible_keys is None else visible_keys
        if visible_keys:
            self.gallery_empty.place_forget()
            return
        if self.records:
            text = "没有符合当前筛选的作品\n请调整关键词或筛选范围"
        elif self.busy:
            text = "正在查找作品\n首个匹配结果会自动出现在这里"
        else:
            text = "当前摄影师尚无已验证作品\n点击“检索作品”建立准确索引"
        self.gallery_empty.configure(text=text)
        self.gallery_empty.place(relx=0.5, rely=0.42, anchor=tk.CENTER)

    def _choose_output_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_dir_var.get() or str(Path.home()))
        if selected:
            self.output_dir_var.set(selected)
            self._load_existing_records()

    def _on_photographer_changed(self, *_args: object) -> None:
        website_url = self.website_url_var.get().strip()
        if not website_url:
            return
        if self.photographer_var.get().strip() == self.website_url_name:
            return
        self._set_auto_website_url("", "")

    def _on_website_url_changed(self, *_args: object) -> None:
        if self._suppress_website_trace:
            return
        if self.website_url_var.get().strip() != self.auto_website_url:
            self.auto_website_url = ""
            self.auto_website_name = ""
        self.website_url_name = self.photographer_var.get().strip() if self.website_url_var.get().strip() else ""

    def _set_auto_website_url(self, url: str, photographer: str) -> None:
        self.auto_website_url = url.strip()
        self.auto_website_name = photographer.strip() if self.auto_website_url else ""
        self.website_url_name = self.auto_website_name
        self._suppress_website_trace = True
        try:
            self.website_url_var.set(self.auto_website_url)
        finally:
            self._suppress_website_trace = False

    def _open_output_dir(self) -> None:
        path = Path(self.output_dir_var.get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def _start_archive(self) -> None:
        """Capture Tk state on the UI thread and start a cancellable search worker.

        All background communication returns through ``ui_queue``; no worker reads
        or mutates Tk variables. @codex-comment approved
        """

        if self.busy:
            return
        photographer = self.photographer_var.get().strip()
        if not photographer:
            messagebox.showwarning("缺少摄影师", "请先输入摄影师名字。")
            return
        website_url = self.website_url_var.get().strip()
        if website_url and self.website_url_name != photographer:
            website_url = ""
            self._set_auto_website_url("", "")
        output_dir_text = self.output_dir_var.get().strip()
        if not output_dir_text:
            messagebox.showwarning("缺少归档目录", "请先选择归档目录。")
            return
        try:
            limit = int(self.limit_var.get())
            download_limit = int(self.download_limit_var.get())
            min_edge = int(self.min_edge_var.get())
        except (tk.TclError, TypeError, ValueError):
            messagebox.showwarning("检索设置无效", "收录、下载和图片尺寸必须是整数。")
            return
        if not 1 <= limit <= 5000 or not 0 <= download_limit <= 5000 or not 0 <= min_edge <= 10000:
            messagebox.showwarning("检索设置无效", "请检查收录、下载和最短长边的数值范围。")
            return
        output_dir = Path(output_dir_text).expanduser()
        download = self.download_var.get()
        self.loaded_search_name = photographer
        self.loaded_database_path = str((output_dir / "photo_archive.db").resolve())
        self.active_photographer = photographer
        self.cancel_event = threading.Event()
        self.issue_count = 0
        self.search_started_at = time.perf_counter()
        self._set_busy(True)
        self.phase_var.set("正在检索")
        if website_url:
            self.detail_var.set(f"正在扫描官网 {website_url}，找到图片会立即显示。")
        else:
            self.detail_var.set(f"正在自动查找 {photographer} 的官网；找到后会扫描作品图。")
        self._set_progress(5)
        self._start_indeterminate_progress()
        self._log(f"search started: {photographer}")
        self.update_idletasks()
        self._clear_live_results()
        self._save_preferences()
        self.worker_thread = threading.Thread(
            target=self._archive_worker,
            args=(photographer, output_dir, website_url, limit, download_limit, min_edge, download),
            daemon=True,
        )
        self.worker_thread.start()

    def _archive_worker(
        self,
        photographer: str,
        output_dir: Path,
        website_url: str,
        limit: int,
        download_limit: int,
        min_edge: int,
        download: bool,
    ) -> None:
        try:
            summary = archive_photographer(
                photographer,
                output_dir=output_dir,
                website_url=website_url,
                limit=limit,
                download_limit=download_limit,
                min_long_edge=min_edge,
                download=download,
                callback=lambda event, payload: self.ui_queue.put((event, payload)),
                cancel_event=self.cancel_event,
            )
            self.ui_queue.put(("done", {"summary": summary}))
        except SearchCancelled:
            self.ui_queue.put(("cancelled", {}))
        except Exception as exc:
            self.ui_queue.put(("error", {"message": str(exc)}))

    def _cancel_archive(self) -> None:
        if not self.busy or self.cancel_event.is_set():
            return
        self.cancel_event.set()
        self.cancel_button.configure(state=tk.DISABLED)
        self.phase_var.set("正在停止")
        self.detail_var.set("等待当前网络请求结束；已经找到的作品会保留。")
        self._log("cancel requested")

    def _download_selected_record(self) -> None:
        record = self._selected_record()
        if not record:
            messagebox.showwarning("未选择图片", "请先在左侧列表里选择一条记录。")
            return
        self._start_record_download([record])

    def _download_current_list(self) -> None:
        records = [self._record_for_source_key(key) for key in self._visible_source_keys()]
        records = [record for record in records if record is not None]
        if not records:
            messagebox.showwarning("没有记录", "当前筛选结果中没有可下载记录。")
            return
        self._start_record_download(records)

    def _start_record_download(self, records: list[PhotoRecord]) -> None:
        allowed = [
            record
            for record in records
            if record.source in {"wikimedia_commons", "website"} and record.image_url and record.source_key not in self.skipped_records
        ]
        if not allowed:
            messagebox.showwarning("无法下载", "当前列表没有可直接下载的公开来源图片。")
            return
        output_dir = Path(self.output_dir_var.get()).expanduser()
        self.cancel_event = threading.Event()
        self.search_started_at = time.perf_counter()
        self._set_busy(True)
        self.phase_var.set("正在下载")
        self.detail_var.set(f"准备下载 {len(allowed)} 张图片。")
        self._stop_indeterminate_progress()
        self._set_progress(0)
        self.worker_thread = threading.Thread(target=self._download_records_worker, args=(allowed, output_dir), daemon=True)
        self.worker_thread.start()

    def _download_records_worker(self, records: list[PhotoRecord], output_dir: Path) -> None:
        downloaded = 0
        failed = 0
        store = ArchiveStore(output_dir / "photo_archive.db")
        try:
            for index, record in enumerate(records, start=1):
                if self.cancel_event.is_set():
                    raise SearchCancelled("Download cancelled by user.")
                self.ui_queue.put(("download_progress", {"current": index, "total": len(records), "title": record.title}))
                try:
                    saved = download_record(
                        record,
                        output_dir,
                        store,
                        callback=lambda event, payload: self.ui_queue.put((event, payload)),
                        cancel_event=self.cancel_event,
                    )
                    self.ui_queue.put(("record_updated", {"record": saved}))
                    if saved.local_path:
                        downloaded += 1
                except SearchCancelled:
                    raise
                except Exception as exc:
                    failed += 1
                    self.ui_queue.put(("download_error", {"title": record.title, "message": str(exc), "current": index, "total": len(records)}))
            self.ui_queue.put(("batch_done", {"downloaded": downloaded, "failed": failed}))
        except SearchCancelled:
            self.ui_queue.put(("cancelled", {"downloaded": downloaded, "failed": failed}))

    def _drain_queue(self) -> None:
        """Apply a bounded event batch so network bursts cannot starve Tk redraws.

        Work is limited by both event count and wall-clock budget; backlog schedules
        the next slice immediately. @codex-comment approved
        """

        started = time.perf_counter()
        processed = 0
        try:
            while processed < UI_EVENTS_PER_TICK and time.perf_counter() - started < UI_TICK_BUDGET_SECONDS:
                try:
                    event, payload = self.ui_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_event(event, payload)
                except Exception:
                    self.report_callback_exception(*sys.exc_info())
                processed += 1
        finally:
            try:
                exists = bool(self.winfo_exists())
            except tk.TclError:
                exists = False
            if exists:
                self.after(1 if not self.ui_queue.empty() else 50, self._drain_queue)

    def _handle_event(self, event: str, payload: dict) -> None:
        if event == "shutdown_complete":
            self.destroy()
        elif event == "status":
            self.phase_var.set("连接来源")
            self.detail_var.set(str(payload.get("message", "")))
            self._log(str(payload.get("message", "")))
        elif event == "official_site_search":
            self.phase_var.set("查找官网")
            self.detail_var.set(str(payload.get("message", "正在查找官网。")))
            self._log(str(payload.get("message", "")))
        elif event == "official_site_found":
            self.phase_var.set("找到官网")
            self.detail_var.set(f"{payload.get('label') or '官网'}: {payload.get('url')}")
            self._set_auto_website_url(str(payload.get("url") or ""), self.active_photographer)
            self._log(f"official site found: {payload.get('label')} {payload.get('url')}")
            self._set_stage_progress(12)
        elif event == "official_site_rejected":
            self.phase_var.set("排除错误官网")
            self.detail_var.set(str(payload.get("message", "官网匹配置信度不足，已排除。")))
            self._log(str(payload.get("message", "official site rejected")))
        elif event == "official_site_missing":
            self.phase_var.set("未找到官网")
            self.detail_var.set(str(payload.get("message", "未找到官网，改用开放图库。")))
            self._log(str(payload.get("message", "")))
            self._set_stage_progress(12)
        elif event == "source_index":
            self.phase_var.set("发现作品页")
            self.detail_var.set(str(payload.get("message", "正在读取网站索引。")))
            self._log(str(payload.get("message", "")))
            self._set_stage_progress(16)
        elif event == "source_collection":
            series = int(payload.get("series") or 0)
            expected = int(payload.get("expected") or 0)
            self.phase_var.set("建立作品目录")
            self.detail_var.set(f"已发现 {series} 个公开作品系列，网站声明约 {expected} 张图片；正在按系列深入检索。")
            self._log(str(payload.get("message", "")))
            self._set_stage_progress(17)
        elif event == "source_render":
            self.phase_var.set("渲染动态画廊")
            render_url = str(payload.get("url") or "")
            self.detail_var.set(f"正在补全动态作品页：{render_url}" if render_url else "正在补全动态公开画廊。")
            self._log(str(payload.get("message", "")))
            self._set_stage_progress(18)
        elif event == "source_notice":
            self.detail_var.set(str(payload.get("message", "来源提示")))
            self._log(str(payload.get("message", "")))
        elif event == "source_blocked":
            self._log(str(payload.get("message", "robots.txt blocked a page")))
        elif event == "source_page":
            self.phase_var.set("扫描作品页")
            current = int(payload.get("current") or 0)
            total = max(1, int(payload.get("total") or 1))
            queued = int(payload.get("queued") or 0)
            accepted = int(payload.get("accepted") or 0)
            series = int(payload.get("series") or 0)
            expected = int(payload.get("expected") or 0)
            page_errors = int(payload.get("page_errors") or 0)
            collection = f"；{series} 个系列，预计 {expected} 张" if series and expected else ""
            failures = f"；页面失败 {page_errors}" if page_errors else ""
            self.detail_var.set(f"扫描第 {current}/{total} 页，队列 {queued}，已收录 {accepted}{collection}{failures}。")
            self._log(str(payload.get("message", "")))
            self._set_stage_progress(18 + min(1.0, current / total) * 66)
        elif event == "source_complete":
            pages = int(payload.get("pages") or 0)
            accepted = int(payload.get("accepted") or 0)
            expected = int(payload.get("expected") or 0)
            page_errors = int(payload.get("page_errors") or 0)
            limit_text = "，已达到收录上限" if payload.get("limit_reached") else ""
            expected_text = f"，网站声明约 {expected} 张" if expected else ""
            self.phase_var.set("官网扫描完成")
            self.detail_var.set(f"扫描 {pages} 页，收录 {accepted} 张{expected_text}，页面失败 {page_errors}{limit_text}。")
            self._log(str(payload.get("message", "")))
        elif event == "source_retry":
            self.phase_var.set("等待重试")
            self.detail_var.set(str(payload.get("message", "网络请求正在重试。")))
            self._log(str(payload.get("message", "")))
        elif event == "search_progress":
            self._update_search_progress(payload)
        elif event == "record_found":
            record_data = payload.get("record")
            if isinstance(record_data, dict):
                record = PhotoRecord(**record_data)
                self.skipped_records.discard(record.source_key)
                self._add_or_update_record(record, select_if_first=True)
            self._update_search_progress(payload)
            self._log(f"found: {payload.get('title')} ({payload.get('resolution')})")
        elif event == "record_persisted":
            self.phase_var.set("保存结果")
        elif event == "record_skipped":
            self._update_search_progress(payload)
            self._log(f"skipped: {payload.get('title')} ({payload.get('resolution')}) {payload.get('reason')}")
        elif event == "record_rejected":
            self._update_search_progress(payload)
            self._log(f"rejected: {payload.get('title')} {payload.get('reason')} (match {payload.get('match_score')})")
        elif event == "source_error":
            self.detail_var.set("当前来源连接失败，已记录错误。")
            self._add_issue()
            self._log(f"source error: {payload.get('source')}: {payload.get('message')}")
        elif event == "record_error":
            self.detail_var.set("部分记录解析失败，已跳过并继续。")
            self._add_issue()
            self._log(f"record error: {payload.get('title')}: {payload.get('message')}")
        elif event == "download_error":
            self.detail_var.set(f"下载失败，已继续：{payload.get('title')}")
            self._add_issue()
            self._log(f"download error: {payload.get('title')}: {payload.get('message')}")
        elif event == "download_progress":
            current = int(payload.get("current") or 0)
            total = max(1, int(payload.get("total") or 1))
            self.phase_var.set("正在下载")
            self.detail_var.set(f"{current}/{total} {payload.get('title')}")
            self._set_progress((current - 1) / total * 100)
        elif event == "bytes":
            total = int(payload.get("total") or 0)
            done = int(payload.get("done") or 0)
            if total:
                self.detail_var.set(f"下载中 {done / total * 100:.1f}%：{payload.get('title')}")
        elif event == "downloaded":
            self._log("saved: " + str(payload.get("path", "")))
        elif event == "duplicate":
            self._log(f"{payload.get('kind')} duplicate: {payload.get('title')} -> {payload.get('duplicate_of')}")
        elif event == "record_updated":
            record = payload.get("record")
            if isinstance(record, PhotoRecord):
                self._add_or_update_record(record, select_if_first=False)
                self.downloaded_var.set(f"下载 {sum(1 for item in self.records.values() if item.local_path)}")
        elif event == "preview_bytes":
            self._apply_remote_preview(payload)
        elif event == "preview_error":
            if payload.get("token") == self.preview_token:
                self.preview_label.configure(image="", text="无法加载远程预览")
        elif event == "thumbnail_bytes":
            self._apply_thumbnail(payload)
        elif event == "thumbnail_error":
            self.thumbnail_loading.discard(str(payload.get("source_key") or ""))
            self._add_issue()
            self._log(f"thumbnail error: {payload.get('title')}: {payload.get('message')}")
        elif event == "study_image":
            callback = self.study_callbacks.pop(int(payload.get("request_id") or 0), None)
            if callback:
                try:
                    callback(payload)
                except tk.TclError:
                    pass
        elif event == "contact_progress":
            current = int(payload.get("current") or 0)
            total = max(1, int(payload.get("total") or 1))
            self.phase_var.set("正在导出联系表")
            self.detail_var.set(f"已准备 {current}/{total} 张；无法预览 {payload.get('failed') or 0} 张。")
            self._set_progress(current / total * 100)
        elif event == "contact_done":
            self.contact_sheet_button.configure(state=tk.NORMAL)
            paths = [str(path) for path in payload.get("paths") or []]
            self.phase_var.set("联系表已导出")
            self.detail_var.set(f"已生成 {len(paths)} 页；无法预览 {payload.get('failed') or 0} 张。")
            self._set_progress(100)
            for path in paths:
                self._log(f"contact sheet saved: {path}")
        elif event == "contact_error":
            self.contact_sheet_button.configure(state=tk.NORMAL)
            self.phase_var.set("联系表导出失败")
            self.detail_var.set(str(payload.get("message") or "无法生成联系表。"))
            self._add_issue()
            self._log(f"contact sheet error: {payload.get('message')}")
        elif event == "log":
            self._log(str(payload.get("message", "")))
        elif event == "batch_done":
            self._set_busy(False)
            self._stop_indeterminate_progress()
            self.phase_var.set("下载完成")
            self.detail_var.set(f"成功 {payload.get('downloaded')}，失败 {payload.get('failed')}。")
            self._set_progress(100)
            self._schedule_result_summary_refresh()
        elif event == "cancelled":
            self._set_busy(False)
            self._stop_indeterminate_progress()
            self.phase_var.set("已停止")
            self.detail_var.set(f"已保留 {len(self.records)} 条结果；可以继续浏览或重新检索。")
            self._set_progress(min(95, float(self.progress["value"])))
            self._schedule_result_summary_refresh()
        elif event == "done":
            summary = payload["summary"]
            self._set_busy(False)
            self._stop_indeterminate_progress()
            self.phase_var.set("归档完成")
            self.detail_var.set(f"收录 {summary.saved} 条，下载 {summary.downloaded} 张，精确重复 {summary.exact_duplicates}，近似重复 {summary.near_duplicates}。")
            self._set_progress(100)
            self.downloaded_var.set(f"下载 {sum(1 for item in self.records.values() if item.local_path)}")
            self._schedule_result_summary_refresh()
        elif event == "error":
            self._set_busy(False)
            self._stop_indeterminate_progress()
            self.phase_var.set("任务失败")
            self.detail_var.set(str(payload.get("message", "")))
            self._add_issue()
            self._log("error: " + str(payload.get("message", "")))
            self._schedule_result_summary_refresh()

    def _update_search_progress(self, payload: dict) -> None:
        accepted = int(payload.get("accepted") or len(self.records))
        seen = int(payload.get("seen") or 0)
        low_resolution = int(payload.get("low_resolution") or 0)
        irrelevant = int(payload.get("irrelevant") or 0)
        duplicates = int(payload.get("duplicates") or 0)
        unknown = int(payload.get("unknown") or 0)
        page_errors = int(payload.get("page_errors") or 0)
        skipped = low_resolution + irrelevant
        rejected = int(payload.get("rejected") or 0)
        self.phase_var.set("正在检索")
        detail = f"检查 {seen}，收录 {accepted}；确认低于尺寸 {low_resolution}，装饰资源 {irrelevant}，重复候选 {duplicates}"
        if unknown:
            detail += f"，尺寸待下载核验 {unknown}"
        if rejected:
            detail += f"，匹配不足 {rejected}"
        if page_errors:
            detail += f"，页面失败 {page_errors}"
        self.detail_var.set(detail + "。")
        self.found_var.set(f"收录 {accepted}")
        self.checked_var.set(f"检查 {seen}")
        self.skipped_var.set(f"排除 {skipped + rejected}")

    def _clear_current_cache(self) -> None:
        search_name = self.photographer_var.get().strip()
        if not search_name:
            messagebox.showwarning("缺少摄影师", "请先输入摄影师名字。")
            return
        db_path = Path(self.output_dir_var.get()).expanduser() / "photo_archive.db"
        if not db_path.exists():
            self._clear_live_results()
            self.phase_var.set("没有缓存")
            self.detail_var.set("当前归档目录没有数据库。")
            return
        deleted = ArchiveStore(db_path).delete_search(search_name)
        self._clear_live_results()
        self.phase_var.set("缓存已清空")
        self.detail_var.set(f"已删除 {search_name} 的 {deleted} 条缓存记录。")
        self._log(f"cache cleared: {search_name}, deleted {deleted} records")

    def _load_existing_records(self, keep_status: bool = False) -> None:
        """Refresh the current library without discarding reusable thumbnails.

        A full reset occurs only when the photographer or database changes. Otherwise
        rows are reconciled by source key and selection is restored. @codex-comment approved
        """

        db_path = Path(self.output_dir_var.get()).expanduser() / "photo_archive.db"
        search_name = self.photographer_var.get().strip()
        database_key = str(db_path.resolve())
        same_library = self.loaded_search_name == search_name and self.loaded_database_path == database_key
        selected = self._selected_record()
        selected_key = selected.source_key if selected else ""
        if not same_library:
            self._clear_tree()
        self.loaded_search_name = search_name
        self.loaded_database_path = database_key
        if not db_path.exists():
            if same_library:
                self._clear_tree()
            if not keep_status:
                self.phase_var.set("没有本地记录")
                self.detail_var.set("当前归档目录还没有数据库。")
            self._refresh_history()
            self._update_result_count()
            self._set_gallery_empty_state()
            return
        store = ArchiveStore(db_path)
        stored_records = store.list_records(search_name=search_name)
        legacy_unverified = [
            record
            for record in stored_records
            if record.source == "wikimedia_commons" and record.match_confidence <= 0
        ]
        legacy_keys = {record.source_key for record in legacy_unverified}
        records = [record for record in stored_records if record.source_key not in legacy_keys]
        incoming_keys = {record.source_key for record in records}
        if same_library:
            for source_key, iid in list(self.record_iids.items()):
                if source_key in incoming_keys:
                    continue
                self.record_tree.delete(iid)
                self.records.pop(iid, None)
                self.record_iids.pop(source_key, None)
                self.thumbnail_images.pop(source_key, None)
                self.list_thumbnail_images.pop(source_key, None)
                self._destroy_gallery_card(source_key)
                if source_key in self.gallery_order:
                    self.gallery_order.remove(source_key)
        for record in records:
            self._add_or_update_record(record, select_if_first=False, defer_ui=True)
        if selected_key and selected_key in self.record_iids:
            iid = self.record_iids[selected_key]
            self.record_tree.selection_set(iid)
            self.record_tree.focus(iid)
            self._show_selected_record()
        elif records:
            self._select_gallery_record(records[0].source_key)
        if records and not keep_status:
            self.phase_var.set("已载入本地记录")
            self.detail_var.set(f"已载入 {len(records)} 条记录。")
        elif legacy_unverified and not keep_status:
            self.phase_var.set("需要重新检索")
            self.detail_var.set(f"已隐藏 {len(legacy_unverified)} 条旧版未核验结果；点击“检索作品”重新建立准确索引。")
        self.found_var.set(f"收录 {len(records)}")
        self.downloaded_var.set(f"下载 {sum(1 for record in records if record.local_path)}")
        self._refresh_history()
        self._apply_result_filter()

    def _clear_live_results(self) -> None:
        self._clear_tree()
        self.preview_token += 1
        self.preview_image = None
        self.preview_label.configure(image="", text="正在检索，找到图片后会逐条显示。")
        self.detail_text.configure(state=tk.NORMAL)
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.configure(state=tk.DISABLED)
        self.found_var.set("收录 0")
        self.checked_var.set("检查 0")
        self.skipped_var.set("排除 0")
        self.downloaded_var.set("下载 0")
        self.issue_var.set("问题 0")
        self.result_filter_var.set("")
        self.result_scope_var.set(RESULT_SCOPE_OPTIONS[0][0])
        self.gallery_page_index = 0
        self._update_result_count()
        self._set_gallery_empty_state()

    def _clear_tree(self) -> None:
        existing_items = tuple(self.records)
        if existing_items:
            self.record_tree.delete(*existing_items)
        for source_key in list(self.gallery_cards):
            self._destroy_gallery_card(source_key)
        self.records = {}
        self.record_iids = {}
        self.skipped_records = set()
        self.record_index = 0
        self.thumbnail_images = OrderedDict()
        self.list_thumbnail_images = OrderedDict()
        self.thumbnail_loading = set()
        self.gallery_order = []
        self.gallery_page_index = 0
        self.selected_source_key = ""
        self.compare_source_keys = []
        self._clear_record_selection("暂无选中作品")
        with self.study_cache_lock:
            self.study_image_cache.clear()
            self.study_cache_bytes = 0
        self._update_result_count()
        self._set_gallery_empty_state()

    def _add_or_update_record(self, record: PhotoRecord, select_if_first: bool, *, defer_ui: bool = False) -> None:
        existing_iid = self.record_iids.get(record.source_key)
        image = self.list_thumbnail_images.get(record.source_key)
        if existing_iid is None:
            iid = str(self.record_index)
            self.record_index += 1
            self.records[iid] = record
            self.record_iids[record.source_key] = iid
            self.gallery_order.append(record.source_key)
            item_options = {
                "iid": iid,
                "text": record.title,
                "values": self._tree_values(record),
                "tags": (self._record_tag(record),),
            }
            if image is not None:
                item_options["image"] = image
            self.record_tree.insert("", tk.END, **item_options)
            self._sync_tree_record_visibility(iid, record)
            if not defer_ui:
                self._schedule_gallery_reflow()
                self._schedule_result_summary_refresh()
            if select_if_first and len(self.records) == 1:
                self.record_tree.selection_set(iid)
                self.record_tree.focus(iid)
                self.record_tree.see(iid)
                self._show_selected_record()
            return
        self.records[existing_iid] = record
        if record.source_key in self.gallery_cards:
            self._ensure_gallery_card(record, schedule_reflow=False)
        item_options = {
            "text": record.title,
            "values": self._tree_values(record),
            "tags": (self._record_tag(record),),
        }
        if image is not None:
            item_options["image"] = image
        self.record_tree.item(existing_iid, **item_options)
        self._sync_tree_record_visibility(existing_iid, record)
        if not defer_ui:
            self._schedule_gallery_reflow()
            self._schedule_result_summary_refresh()

    def _iid_for_record(self, record: PhotoRecord) -> str | None:
        return self.record_iids.get(record.source_key)

    def _tree_values(self, record: PhotoRecord) -> tuple[str, str, str, str, str]:
        if record.source_key in self.skipped_records:
            status = "低于阈值"
        elif record.duplicate_of:
            status = "精确重复"
        elif record.near_duplicate_of:
            status = "近似重复"
        elif record.local_path:
            status = "已下载"
        else:
            status = "可预览"
        match = f"{record.match_confidence}%" if record.match_confidence else "待核验"
        source = {"website": "官网", "wikimedia_commons": "开放图库"}.get(record.source, record.source.replace("_", " "))
        return (status, match, record.resolution or "待确认", record.collection_title or "-", source)

    def _record_tag(self, record: PhotoRecord) -> str:
        if record.source_key in self.skipped_records:
            return "skipped"
        if record.duplicate_of or record.near_duplicate_of:
            return "duplicate"
        if record.local_path:
            return "downloaded"
        return "pending"

    def _start_thumbnail_load(self, record: PhotoRecord) -> None:
        if Image is None or ImageTk is None:
            return
        source_key = record.source_key
        if source_key in self.thumbnail_images:
            self.thumbnail_images.move_to_end(source_key)
            if source_key in self.list_thumbnail_images:
                self.list_thumbnail_images.move_to_end(source_key)
            return
        if source_key in self.thumbnail_loading:
            return
        path = Path(record.local_path) if record.local_path else None
        url = record.thumb_url or record.image_url
        if (path is None or not path.exists()) and not url:
            return
        self.thumbnail_loading.add(source_key)
        self.thumbnail_executor.submit(self._thumbnail_worker, source_key, record.title, path, url)

    def _thumbnail_worker(self, source_key: str, title: str, path: Path | None, url: str) -> None:
        try:
            if self.shutdown_event.is_set():
                return
            if path and path.exists():
                with Image.open(path) as image:
                    image.thumbnail(THUMB_SIZE)
                    rendered = BytesIO()
                    image.convert("RGB").save(rendered, format="JPEG", quality=86)
                    content = rendered.getvalue()
            elif url:
                with open_public_stream(
                    url,
                    headers={"User-Agent": "PhotoArchiveTool/0.1"},
                    timeout=(5, 12),
                    label="Thumbnail request",
                    cancel_event=self.shutdown_event,
                    total_timeout=30,
                ) as response:
                    response.raise_for_status()
                    declared_size = int(response.headers.get("Content-Length") or 0)
                    if declared_size > MAX_THUMBNAIL_BYTES:
                        raise ValueError(f"thumbnail source is too large ({declared_size} bytes)")
                    chunks: list[bytes] = []
                    received = 0
                    for chunk in response.iter_content(256 * 1024):
                        if self.shutdown_event.is_set():
                            return
                        if not chunk:
                            continue
                        received += len(chunk)
                        if received > MAX_THUMBNAIL_BYTES:
                            raise ValueError("thumbnail source exceeded size limit")
                        chunks.append(chunk)
                    with Image.open(BytesIO(b"".join(chunks))) as image:
                        image.thumbnail(THUMB_SIZE)
                        rendered = BytesIO()
                        image.convert("RGB").save(rendered, format="JPEG", quality=86)
                        content = rendered.getvalue()
            else:
                return
            self.ui_queue.put(("thumbnail_bytes", {"source_key": source_key, "title": title, "content": content}))
        except Exception as exc:
            self.ui_queue.put(("thumbnail_error", {"source_key": source_key, "title": title, "message": str(exc)}))

    def _apply_thumbnail(self, payload: dict) -> None:
        source_key = str(payload.get("source_key") or "")
        self.thumbnail_loading.discard(source_key)
        if not source_key or Image is None or ImageTk is None:
            return
        if source_key not in self.record_iids:
            return
        try:
            with Image.open(BytesIO(payload["content"])) as img:
                gallery_image = ImageTk.PhotoImage(img.copy())
                list_copy = img.copy()
                list_copy.thumbnail(LIST_THUMB_SIZE)
                list_image = ImageTk.PhotoImage(list_copy)
            self.thumbnail_images.pop(source_key, None)
            self.list_thumbnail_images.pop(source_key, None)
            self.thumbnail_images[source_key] = gallery_image
            self.list_thumbnail_images[source_key] = list_image
            iid = self.record_iids.get(source_key)
            if iid:
                self.record_tree.item(iid, image=list_image)
            card = self.gallery_cards.get(source_key)
            if card:
                card["image"].configure(image=gallery_image, text="", bg="#20272E")
            self._trim_thumbnail_cache()
        except Exception as exc:
            self._log(f"thumbnail render error: {payload.get('title')}: {exc}")

    def _trim_thumbnail_cache(self) -> None:
        protected = set(self.gallery_cards)
        if self.selected_source_key:
            protected.add(self.selected_source_key)
        while len(self.thumbnail_images) > MAX_THUMBNAIL_CACHE_ITEMS:
            evict_key = next((key for key in self.thumbnail_images if key not in protected), "")
            if not evict_key:
                break
            self.thumbnail_images.pop(evict_key, None)
            self.list_thumbnail_images.pop(evict_key, None)
            iid = self.record_iids.get(evict_key)
            if iid and self.record_tree.exists(iid):
                self.record_tree.item(iid, image="")

    def _request_study_image(self, record: PhotoRecord, callback) -> None:
        self.study_request_index += 1
        request_id = self.study_request_index
        self.study_callbacks[request_id] = callback
        cached = self._get_cached_study_image(record.source_key)
        if cached:
            self.ui_queue.put(("study_image", {"request_id": request_id, "content": cached}))
            return
        self.study_executor.submit(self._study_image_worker, request_id, record)

    def _study_image_worker(self, request_id: int, record: PhotoRecord) -> None:
        try:
            content = self._read_record_content(record, prefer_original=True)
            self._cache_study_image(record.source_key, content)
            self.ui_queue.put(("study_image", {"request_id": request_id, "content": content}))
        except Exception as exc:
            self.ui_queue.put(("study_image", {"request_id": request_id, "message": str(exc)}))

    def _get_cached_study_image(self, source_key: str) -> bytes | None:
        with self.study_cache_lock:
            content = self.study_image_cache.get(source_key)
            if content is not None:
                self.study_image_cache.move_to_end(source_key)
            return content

    def _cache_study_image(self, source_key: str, content: bytes) -> None:
        if not content or len(content) > MAX_STUDY_CACHE_BYTES:
            return
        with self.study_cache_lock:
            previous = self.study_image_cache.pop(source_key, None)
            if previous is not None:
                self.study_cache_bytes -= len(previous)
            while self.study_image_cache and (
                len(self.study_image_cache) >= MAX_STUDY_CACHE_ITEMS
                or self.study_cache_bytes + len(content) > MAX_STUDY_CACHE_BYTES
            ):
                _key, evicted = self.study_image_cache.popitem(last=False)
                self.study_cache_bytes -= len(evicted)
            self.study_image_cache[source_key] = content
            self.study_cache_bytes += len(content)

    def _read_record_content(self, record: PhotoRecord, *, prefer_original: bool) -> bytes:
        if record.local_path:
            path = Path(record.local_path)
            if path.exists():
                size = path.stat().st_size
                if size > MAX_STUDY_IMAGE_BYTES:
                    raise ValueError(f"image exceeds preview size limit ({size} bytes)")
                return path.read_bytes()
        urls = (record.image_url, record.thumb_url) if prefer_original else (record.thumb_url, record.image_url)
        url = next((value for value in urls if value), "")
        if not url:
            raise ValueError("record has no preview URL")
        with open_public_stream(
            url,
            headers={"User-Agent": "PhotoArchiveTool/0.1"},
            timeout=(6, 20),
            label="Study preview request",
            cancel_event=self.shutdown_event,
            total_timeout=45,
        ) as response:
            response.raise_for_status()
            declared_size = int(response.headers.get("Content-Length") or 0)
            if declared_size > MAX_STUDY_IMAGE_BYTES:
                raise ValueError(f"image exceeds preview size limit ({declared_size} bytes)")
            chunks: list[bytes] = []
            received = 0
            for chunk in response.iter_content(512 * 1024):
                if self.shutdown_event.is_set():
                    raise SearchCancelled("application is closing")
                if not chunk:
                    continue
                received += len(chunk)
                if received > MAX_STUDY_IMAGE_BYTES:
                    raise ValueError("image exceeded preview size limit")
                chunks.append(chunk)
            return b"".join(chunks)

    def _open_fullscreen_viewer(self) -> None:
        selected = self._selected_record()
        if not selected:
            return
        records = [self._record_for_source_key(key) for key in self._visible_source_keys()]
        visible_records = [record for record in records if record is not None]
        if not visible_records:
            visible_records = [selected]
        try:
            index = next(index for index, record in enumerate(visible_records) if record.source_key == selected.source_key)
        except StopIteration:
            visible_records.insert(0, selected)
            index = 0
        FullscreenStudyViewer(self, visible_records, index)

    def _toggle_selected_comparison(self) -> None:
        record = self._selected_record()
        if not record:
            return
        source_key = record.source_key
        if source_key in self.compare_source_keys:
            self.compare_source_keys.remove(source_key)
            self.phase_var.set("已移出对比")
            self.detail_var.set(f"对比区现有 {len(self.compare_source_keys)} 张作品。")
        else:
            if len(self.compare_source_keys) >= 2:
                self.compare_source_keys.pop(0)
            self.compare_source_keys.append(source_key)
            self.phase_var.set("已加入对比")
            self.detail_var.set(f"已将《{self._compact_title(record.title, 28)}》加入对比区。")
        self._update_gallery_selection()
        if len(self.compare_source_keys) == 2 and source_key in self.compare_source_keys:
            self.after(100, self._open_compare_window)

    def _open_compare_window(self) -> None:
        records = [self._record_for_source_key(key) for key in self.compare_source_keys]
        comparison = [record for record in records if record is not None]
        if len(comparison) != 2:
            self.phase_var.set("请选择两张作品")
            self.detail_var.set("将两张作品加入对比区后即可并排研究。")
            return
        CompareStudyWindow(self, comparison)

    def _record_for_source_key(self, source_key: str) -> PhotoRecord | None:
        iid = self.record_iids.get(source_key)
        return self.records.get(iid or "")

    def _export_contact_sheet(self) -> None:
        records = [self._record_for_source_key(key) for key in self._visible_source_keys()]
        visible_records = [record for record in records if record is not None]
        if not visible_records:
            self.phase_var.set("没有可导出的作品")
            self.detail_var.set("当前筛选结果为空。")
            return
        photographer = self.photographer_var.get().strip() or "photographic-study"
        safe_name = "".join(character if character.isalnum() or character in " -_" else "_" for character in photographer).strip()
        destination = filedialog.asksaveasfilename(
            title="导出联系表",
            defaultextension=".jpg",
            filetypes=[("JPEG image", "*.jpg")],
            initialfile=f"{safe_name or 'photographic-study'}-contact-sheet.jpg",
        )
        if not destination:
            return
        self.contact_sheet_button.configure(state=tk.DISABLED)
        self.phase_var.set("正在导出联系表")
        self.detail_var.set(f"准备 {len(visible_records)} 张作品的缩略图。")
        self.study_executor.submit(self._contact_sheet_worker, visible_records, Path(destination), photographer)

    def _contact_sheet_worker(self, records: list[PhotoRecord], destination: Path, photographer: str) -> None:
        items: list[tuple[PhotoRecord, bytes | None]] = []
        failed = 0
        try:
            for index, record in enumerate(records, start=1):
                if self.shutdown_event.is_set():
                    raise SearchCancelled("application is closing")
                content = self._get_cached_study_image(record.source_key)
                if content is None:
                    try:
                        content = self._read_record_content(record, prefer_original=False)
                    except Exception as exc:
                        failed += 1
                        self.ui_queue.put(("log", {"message": f"contact sheet preview skipped: {record.title}: {exc}"}))
                items.append((record, content))
                self.ui_queue.put(("contact_progress", {"current": index, "total": len(records), "failed": failed}))
            paths = create_contact_sheet_pages(items, destination, photographer)
            self.ui_queue.put(("contact_done", {"paths": [str(path) for path in paths], "failed": failed}))
        except Exception as exc:
            self.ui_queue.put(("contact_error", {"message": str(exc)}))

    def _selected_record(self) -> PhotoRecord | None:
        selection = self.record_tree.selection()
        if not selection:
            return None
        return self.records.get(selection[0])

    def _clear_record_selection(self, preview_text: str) -> None:
        selection = self.record_tree.selection()
        if selection:
            self.record_tree.selection_remove(*selection)
        self.selected_source_key = ""
        self.preview_token += 1
        self.preview_image = None
        self.preview_label.configure(image="", text=preview_text)
        self.detail_text.configure(state=tk.NORMAL)
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.configure(state=tk.DISABLED)
        self._update_gallery_selection()

    def _show_selected_record(self) -> None:
        record = self._selected_record()
        if not record:
            return
        self.selected_source_key = record.source_key
        self._update_gallery_selection()
        self._show_preview(record)
        self._show_details(record)

    def _show_preview(self, record: PhotoRecord) -> None:
        self.preview_token += 1
        token = self.preview_token
        self.preview_image = None
        self.preview_label.configure(image="", text="加载预览...")
        if record.local_path:
            path = Path(record.local_path)
            if path.exists():
                self.preview_executor.submit(self._local_preview_worker, path, token)
                return
        preview_url = record.thumb_url or record.image_url
        if not preview_url:
            self.preview_label.configure(text="没有可预览图片")
            return
        if Image is None or ImageTk is None:
            self.preview_label.configure(text=preview_url)
            return
        self.preview_executor.submit(self._remote_preview_worker, preview_url, token)

    def _local_preview_worker(self, path: Path, token: int) -> None:
        try:
            with Image.open(path) as img:
                img.thumbnail(PREVIEW_SIZE)
                rendered = BytesIO()
                img.convert("RGB").save(rendered, format="JPEG", quality=90)
            self.ui_queue.put(("preview_bytes", {"token": token, "content": rendered.getvalue()}))
        except Exception as exc:
            self.ui_queue.put(("preview_error", {"token": token, "message": str(exc)}))

    def _remote_preview_worker(self, url: str, token: int) -> None:
        try:
            if self.shutdown_event.is_set():
                return
            with open_public_stream(
                url,
                headers={"User-Agent": "PhotoArchiveTool/0.1"},
                timeout=(5, 12),
                label="Preview request",
                cancel_event=self.shutdown_event,
                total_timeout=30,
            ) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                received = 0
                for chunk in response.iter_content(512 * 1024):
                    if self.shutdown_event.is_set():
                        return
                    if not chunk:
                        continue
                    received += len(chunk)
                    if received > MAX_THUMBNAIL_BYTES * 2:
                        raise ValueError("preview source exceeded size limit")
                    chunks.append(chunk)
            with Image.open(BytesIO(b"".join(chunks))) as image:
                image.thumbnail(PREVIEW_SIZE)
                rendered = BytesIO()
                image.convert("RGB").save(rendered, format="JPEG", quality=90)
            self.ui_queue.put(("preview_bytes", {"token": token, "content": rendered.getvalue()}))
        except Exception as exc:
            self.ui_queue.put(("preview_error", {"token": token, "message": str(exc)}))

    def _apply_remote_preview(self, payload: dict) -> None:
        if payload.get("token") != self.preview_token:
            return
        if Image is None or ImageTk is None:
            return
        try:
            with Image.open(BytesIO(payload["content"])) as img:
                self.preview_image = ImageTk.PhotoImage(img.copy())
            self.preview_label.configure(image=self.preview_image, text="")
        except Exception as exc:
            self.preview_label.configure(image="", text=f"无法预览：{exc}")

    def _show_details(self, record: PhotoRecord) -> None:
        duplicate_text = ""
        match_text = f"{record.match_confidence}% / {record.match_reason}" if record.match_confidence else "待核验"
        if record.duplicate_of:
            duplicate_text = f"精确重复：{record.duplicate_of}"
        elif record.near_duplicate_of:
            duplicate_text = f"近似重复：{record.near_duplicate_of} / 距离 {record.duplicate_distance}"
        lines = [
            f"标题：{record.title}",
            f"系列 / 项目：{record.collection_title or '-'}",
            f"摄影师查询：{record.search_name}",
            f"身份匹配：{match_text}",
            f"研究评分：{record.rating}/5" if record.rating else "研究评分：未评分",
            f"研究标签：{record.research_tags or '-'}",
            f"尺寸：{record.resolution or '-'}",
            f"作者/上传者：{record.author or '-'}",
            f"许可：{record.license_name or '-'}",
            f"许可链接：{record.license_url or '-'}",
            f"来源页面：{record.page_url or '-'}",
            f"本地文件：{record.local_path or '-'}",
            f"SHA-256：{record.sha256 or '-'}",
            f"dHash：{record.dhash or '-'}",
            duplicate_text,
            "",
            "研究笔记：",
            record.research_note or "-",
            "",
            "注解：",
            record.annotation or "-",
            "",
            "评论 / 来源说明：",
            record.source_comment or record.credit or "-",
            "",
            "拍摄细节：",
            f"日期：{record.shooting_date or '-'}",
            f"相机：{_join(record.camera_make, record.camera_model)}",
            f"镜头：{record.lens_model or '-'}",
            f"曝光：{record.exposure_time or '-'}",
            f"光圈：{record.f_number or '-'}",
            f"ISO：{record.iso or '-'}",
            f"焦距：{record.focal_length or '-'}",
        ]
        self.detail_text.configure(state=tk.NORMAL)
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.insert(tk.END, "\n".join(line for line in lines if line is not None))
        self.detail_text.yview_moveto(0)
        self.detail_text.configure(state=tk.DISABLED)

    def _edit_research_content(self) -> None:
        record = self._selected_record()
        if not record:
            self.phase_var.set("请选择作品")
            self.detail_var.set("选中一张作品后可编辑研究内容。")
            return
        ResearchEditorDialog(
            self,
            record,
            lambda note, tags, rating: self._save_research_content(record, note, tags, rating),
        )

    def _save_research_content(self, record: PhotoRecord, note: str, tags: str, rating: int) -> bool:
        db_path = Path(self.loaded_database_path) if self.loaded_database_path else (
            Path(self.output_dir_var.get()).expanduser() / "photo_archive.db"
        )
        try:
            store = ArchiveStore(db_path)
            updated = store.update_research_content(record.source, record.source_id, note, tags, rating)
            if updated is None:
                record.research_note = note.strip()
                record.research_tags = tags
                record.rating = max(0, min(5, int(rating or 0)))
                updated = store.upsert(record)
        except Exception as exc:
            messagebox.showerror("保存失败", f"无法保存研究内容：{exc}", parent=self)
            return False

        self._add_or_update_record(updated, select_if_first=False)
        iid = self.record_iids.get(updated.source_key)
        if iid:
            self.record_tree.selection_set(iid)
            self.record_tree.focus(iid)
        self._apply_result_filter()
        self._show_details(updated)
        self.phase_var.set("研究内容已保存")
        self.detail_var.set(f"已更新《{self._compact_title(updated.title, 34)}》的评分、标签与笔记。")
        self._log(f"research content saved: {updated.source_key}")
        return True

    def _export_research_record(self) -> None:
        record = self._selected_record()
        if not record:
            self.phase_var.set("请选择作品")
            self.detail_var.set("选中一张作品后可导出研究档案。")
            return
        initial_name = safe_filename(
            f"{record.search_name} - {record.collection_title or record.title}",
            default="photographic-research",
            max_length=90,
        )
        destination = filedialog.asksaveasfilename(
            title="导出研究档案",
            defaultextension=".md",
            filetypes=[("Markdown document", "*.md")],
            initialfile=f"{initial_name}.md",
        )
        if not destination:
            return
        try:
            Path(destination).write_text(render_research_markdown(record), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("导出失败", f"无法写入研究档案：{exc}", parent=self)
            return
        self.phase_var.set("研究档案已导出")
        self.detail_var.set(str(destination))
        self._log(f"research record exported: {destination}")

    def _open_source_page(self) -> None:
        record = self._selected_record()
        if record and record.page_url:
            webbrowser.open(record.page_url)

    def _open_selected_file(self) -> None:
        record = self._selected_record()
        if record and record.local_path and Path(record.local_path).exists():
            os.startfile(record.local_path)

    def _set_progress(self, value: float) -> None:
        self.progress["value"] = max(0, min(100, value))

    def _set_stage_progress(self, value: float) -> None:
        if str(self.progress.cget("mode")) == "indeterminate":
            self._stop_indeterminate_progress()
        self._set_progress(max(float(self.progress["value"]), value))

    def _start_indeterminate_progress(self) -> None:
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)

    def _stop_indeterminate_progress(self) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        self.start_button.configure(state=state)
        self.download_selected_button.configure(state=state)
        self.download_list_button.configure(state=state)
        self.compare_button.configure(state=state)
        self.contact_sheet_button.configure(state=state)
        self.refresh_button.configure(state=state)
        self.clear_cache_button.configure(state=state)
        self.cancel_button.configure(state=tk.NORMAL if busy else tk.DISABLED)

    def _update_elapsed(self) -> None:
        if self.busy and self.search_started_at:
            elapsed = max(0, int(time.perf_counter() - self.search_started_at))
            self.elapsed_var.set(f"用时 {elapsed}秒")
        self.after(250, self._update_elapsed)

    def _add_issue(self) -> None:
        self.issue_count += 1
        self.issue_var.set(f"问题 {self.issue_count}")

    def _on_close(self) -> None:
        if self.closing:
            return
        self._save_preferences()
        self.closing = True
        self.cancel_event.set()
        self.shutdown_event.set()
        self.phase_var.set("\u6b63\u5728\u5b89\u5168\u9000\u51fa")
        self.detail_var.set("\u6b63\u5728\u505c\u6b62\u7f51\u7edc\u8bf7\u6c42\u5e76\u5199\u5165\u5df2\u5b8c\u6210\u7684\u7ed3\u679c\u3002")
        self.start_button.configure(state=tk.DISABLED)
        self.cancel_button.configure(state=tk.DISABLED)
        self.shutdown_thread = threading.Thread(
            target=self._wait_for_background_shutdown,
            name="photo-archive-shutdown",
            daemon=True,
        )
        self.shutdown_thread.start()

    def _wait_for_background_shutdown(self) -> None:
        deadline = time.monotonic() + BACKGROUND_SHUTDOWN_GRACE_SECONDS
        worker = self.worker_thread
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        for executor in (self.thumbnail_executor, self.preview_executor, self.study_executor):
            executor.shutdown(wait=False, cancel_futures=True)
        background_prefixes = ("photo-thumb", "photo-preview", "photo-study")
        for thread in list(threading.enumerate()):
            if thread is threading.current_thread() or not thread.name.startswith(background_prefixes):
                continue
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        self.ui_queue.put(("shutdown_complete", {}))

    def _log(self, message: str) -> None:
        if not message:
            return
        self.pending_log_lines.append(f"{time.strftime('%H:%M:%S')} {message}\n")
        if len(self.pending_log_lines) > MAX_LOG_LINES:
            del self.pending_log_lines[: len(self.pending_log_lines) - MAX_LOG_LINES]
        if self.log_flush_after is None:
            self.log_flush_after = self.after(LOG_FLUSH_DELAY_MS, self._flush_log_lines)

    def _flush_log_lines(self) -> None:
        self.log_flush_after = None
        if not self.pending_log_lines:
            return
        lines = self.pending_log_lines
        self.pending_log_lines = []
        excess = max(0, self.rendered_log_lines + len(lines) - MAX_LOG_LINES)
        if excess:
            self.log_text.delete("1.0", f"{excess + 1}.0")
            self.rendered_log_lines = max(0, self.rendered_log_lines - excess)
        self.log_text.insert(tk.END, "".join(lines))
        self.rendered_log_lines += len(lines)
        if self.log_visible:
            self.log_text.see(tk.END)


def _join(*values: str) -> str:
    text = " ".join(value for value in values if value)
    return text or "-"


def _release_smoke() -> int:
    required_resources = (
        "assets/photo_archive_icons/photo_archive_app.ico",
        "assets/photo_archive_icons/app_icon_48.png",
        "assets/photo_archive_icons/png/search-dark.png",
    )
    return 0 if all(_resource_path(item).is_file() for item in required_resources) else 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--release-smoke"]:
        return _release_smoke()
    if args == ["--version"]:
        return 0
    if args == ["--ui-smoke"]:
        app = PhotoArchiveApp(load_archive=False, preferences_path=None)
        probe = threading.Thread(
            target=lambda: app.shutdown_event.wait(5),
            name="photo-archive-ui-smoke-worker",
            daemon=True,
        )
        app.worker_thread = probe
        probe.start()
        app.after(1200, app._on_close)
        app.mainloop()
        return 0 if not probe.is_alive() else 3
    app = PhotoArchiveApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
