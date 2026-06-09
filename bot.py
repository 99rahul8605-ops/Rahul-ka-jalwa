from dotenv import load_dotenv
load_dotenv()
import os
import re
import sys
import json
import logging
import asyncio
import tempfile
import shutil
import urllib.parse
import subprocess
import time
from pathlib import Path
import gdown
import requests

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Env ───────────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID    = int(os.environ.get("TELEGRAM_API_ID", "0"))
API_HASH  = os.environ.get("TELEGRAM_API_HASH", "")
OWNER_ID  = int(os.environ.get("OWNER_ID", "0"))   # apna Telegram user ID daalo

# ── Settings ──────────────────────────────────────────────────────────────────

SETTINGS_FILE    = "settings.json"
DEFAULT_SETTINGS = {
    "library":      "pyrogram",
    "workers":      4,
    "max_dl_gb":    1.95,          # ~2GB safe limit
    "watermark":    "",            # floating watermark text (empty = disabled)
}

def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                s = json.load(f)
            for k, v in DEFAULT_SETTINGS.items():
                s.setdefault(k, v)
            return s
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()

def save_settings(s: dict):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f, indent=2)

SETTINGS      = load_settings()
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024          # 2 GB — Telegram free limit
MAX_DL_SIZE   = 10 * 1024 * 1024 * 1024         # 10 GB — will be split if > 2GB

# ── Patterns ──────────────────────────────────────────────────────────────────

GDRIVE_PATTERNS = [
    r"https://drive\.google\.com/file/d/([a-zA-Z0-9_-]+)",
    r"https://drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)",
    r"https://drive\.google\.com/uc\?id=([a-zA-Z0-9_-]+)",
    r"https://docs\.google\.com/.*?/d/([a-zA-Z0-9_-]+)",
    r"id=([a-zA-Z0-9_-]+)",
]
FOLDER_PATTERNS = [
    r"https://drive\.google\.com/drive/folders/([a-zA-Z0-9_-]+)",
]
YTDLP_DOMAINS = [
    "youtube.com", "youtu.be", "instagram.com", "twitter.com", "x.com",
    "tiktok.com", "facebook.com", "fb.watch", "reddit.com", "dailymotion.com",
    "vimeo.com", "twitch.tv", "soundcloud.com", "pinterest.com", "pin.it",
    "pinterest.co.uk", "pinterest.in", "streamable.com",
    "bilibili.com", "rumble.com", "odysee.com", "kick.com",
]
MAGNET_PATTERN = re.compile(r"magnet:\?xt=urn:[a-zA-Z0-9]+:[a-fA-F0-9]{32,40}", re.IGNORECASE)
STREAM_EXTS    = {".m3u8", ".m3u", ".mpd", ".f4m"}
VIDEO_EXTS     = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v", ".3gp"}
AUDIO_EXTS     = {".mp3", ".m4a", ".ogg", ".flac", ".wav", ".aac", ".opus"}

HTTP             = requests.Session()
HTTP.headers.update({"User-Agent": "Mozilla/5.0"})
BOT_START_TIME   = time.time()   # used to skip stale messages on startup
_download_lock: asyncio.Lock | None = None
_cancel_event:  asyncio.Event | None = None
_drm_stop_after_current: bool = False   # set by /cancel during DRM batch — stops after current item finishes
_active_tmp_dir: str | None = None   # track current download tmp dir for cleanup
_drm_batch_active: bool = False          # True while a DRM batch loop is running

# ── DRM session state ─────────────────────────────────────────────────────────
# drm_sessions[user_id] = {"state": "awaiting_file"|"awaiting_index", "links": [...]}
drm_sessions: dict[int, dict] = {}

def get_download_lock() -> asyncio.Lock:
    """Return the single global download Lock. Created once, reused across the session."""
    global _download_lock
    if _download_lock is None:
        _download_lock = asyncio.Lock()
    return _download_lock

def get_cancel_event() -> asyncio.Event:
    """Return the single global cancel Event. Created once, reused across the session."""
    global _cancel_event
    if _cancel_event is None:
        _cancel_event = asyncio.Event()
    return _cancel_event


# ── Link detection ────────────────────────────────────────────────────────────

def detect_link_type(text: str):
    text = text.strip()
    if MAGNET_PATTERN.match(text):
        return text, "magnet"
    for p in FOLDER_PATTERNS:
        m = re.search(p, text)
        if m: return m.group(1), "gdrive_folder"
    if "drive.google.com" in text or "docs.google.com" in text:
        for p in GDRIVE_PATTERNS:
            m = re.search(p, text)
            if m: return m.group(1), "gdrive_file"
        return None, "unknown"
    try:
        domain = urllib.parse.urlparse(text).netloc.lower().lstrip("www.")
        if any(domain == d or domain.endswith("." + d) for d in YTDLP_DOMAINS):
            return text, "ytdlp"
    except Exception:
        pass
    if re.match(r"https?://", text):
        path = urllib.parse.urlparse(text).path.lower()
        query = urllib.parse.urlparse(text).query.lower()
        if any(path.endswith(ext) for ext in STREAM_EXTS):
            return text, "ytdlp"
        # Direct file link — URL path ends with a known media/archive extension
        ALL_MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS | {
            ".pdf", ".zip", ".rar", ".7z", ".tar", ".gz",
            ".jpg", ".jpeg", ".png", ".gif", ".webp",
            ".mp4", ".mkv", ".mov", ".avi", ".webm",
            ".mp3", ".m4a", ".ogg", ".flac", ".wav",
            ".docx", ".xlsx", ".pptx", ".txt", ".csv",
        }
        if any(path.endswith(ext) for ext in ALL_MEDIA_EXTS):
            return text, "direct"
        # Download link indicators — treat as direct
        if "mode=download" in query or "/dl/" in path or "download=1" in query:
            return text, "direct"
        # Looks like a webpage — try to scrape video from it
        return text, "stream_page"
    return None, "unknown"


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_real_filename(file_id):
    try:
        resp = HTTP.head(f"https://drive.google.com/uc?id={file_id}&export=download",
                         allow_redirects=True, timeout=10)
        cd = resp.headers.get("Content-Disposition", "")
        m  = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\n]+)', cd, re.IGNORECASE)
        if m:
            name = urllib.parse.unquote(m.group(1).strip().strip('"\''))
            if name: return name
        ext = content_type_to_ext(resp.headers.get("Content-Type", ""))
        if ext: return f"file{ext}"
    except Exception as e:
        logger.warning(f"GDrive filename failed: {e}")
    return None

def get_direct_filename(url: str) -> str:
    try:
        resp = HTTP.head(url, allow_redirects=True, timeout=10)
        cd   = resp.headers.get("Content-Disposition", "")
        m    = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\n]+)', cd, re.IGNORECASE)
        if m:
            name = urllib.parse.unquote(m.group(1).strip().strip('"\''))
            if name: return name
        path = urllib.parse.urlparse(url).path
        name = urllib.parse.unquote(path.rstrip("/").split("/")[-1])
        if name and "." in name: return name
        ext = content_type_to_ext(resp.headers.get("Content-Type", ""))
        return f"file{ext}" if ext else "downloaded_file"
    except Exception:
        pass
    name = urllib.parse.unquote(urllib.parse.urlparse(url).path.rstrip("/").split("/")[-1])
    return name if name else "downloaded_file"

def get_remote_file_size(url: str) -> int:
    try:
        return int(HTTP.head(url, allow_redirects=True, timeout=10).headers.get("Content-Length", 0))
    except Exception:
        return 0

def content_type_to_ext(ct):
    ct = ct.split(";")[0].strip().lower()
    return {
        "application/pdf": ".pdf",
        "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
        "video/mp4": ".mp4", "video/x-matroska": ".mkv", "video/quicktime": ".mov",
        "video/x-msvideo": ".avi", "video/webm": ".webm",
        "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/wav": ".wav", "audio/flac": ".flac",
        "application/zip": ".zip", "application/x-rar-compressed": ".rar",
        "application/x-7z-compressed": ".7z",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "text/plain": ".txt", "text/csv": ".csv", "application/json": ".json",
    }.get(ct, "")

def sniff_extension(filepath):
    sigs = {
        b"%PDF": ".pdf", b"\x89PNG": ".png", b"\xff\xd8\xff": ".jpg",
        b"GIF8": ".gif", b"PK\x03\x04": ".zip", b"Rar!": ".rar",
        b"\x1f\x8b": ".gz", b"ID3": ".mp3", b"fLaC": ".flac",
    }
    try:
        with open(filepath, "rb") as f:
            h = f.read(8)
        for magic, ext in sigs.items():
            if h.startswith(magic): return ext
    except Exception:
        pass
    return ""

def human_size(b):
    if b < 1024**2: return f"{b/1024:.1f} KB"
    if b < 1024**3: return f"{b/1024**2:.1f} MB"
    return f"{b/1024**3:.2f} GB"

def progress_bar(current: int, total: int) -> str:
    """Returns a unified progress line: bar + % + transferred/total + speed placeholder."""
    pct = min(int(current * 100 / total), 100) if total else 0
    filled = pct // 5          # 20-block bar (each block = 5%)
    bar = "█" * filled + "░" * (20 - filled)
    return f"{bar} {pct}%\n📥 {human_size(current)} / {human_size(total)}"

def upload_bar(current: int, total: int) -> str:
    pct = min(int(current * 100 / total), 100) if total else 0
    filled = pct // 5
    bar = "█" * filled + "░" * (20 - filled)
    return f"{bar} {pct}%\n📤 {human_size(current)} / {human_size(total)}"

def fix_filename(fp: Path) -> Path:
    if "." not in fp.name:
        ext = sniff_extension(str(fp))
        if ext:
            new = fp.parent / (fp.name + ext)
            fp.rename(new)
            return new
    return fp


SPLIT_SIZE = 1950 * 1024 * 1024   # 1.95 GB per part — safe under 2GB limit

async def split_and_send(send_fn, edit, fp: Path):
    """Split file into 1.95GB parts and send each one."""
    file_size = fp.stat().st_size

    if file_size <= MAX_FILE_SIZE:
        await send_fn(fp)
        return

    total_parts = (file_size + SPLIT_SIZE - 1) // SPLIT_SIZE
    await edit(f"✂️ File is {human_size(file_size)} — splitting into {total_parts} parts...")

    stem = fp.stem
    ext  = fp.suffix
    part_paths = []

    with open(fp, "rb") as f:
        for i in range(1, total_parts + 1):
            part_name = fp.parent / f"{stem}.part{i:02d}of{total_parts:02d}{ext}"
            chunk     = f.read(SPLIT_SIZE)
            if not chunk:
                break
            with open(part_name, "wb") as pf:
                pf.write(chunk)
            part_paths.append(part_name)
            await edit(f"✂️ Part {i}/{total_parts} ready ({human_size(len(chunk))}). Sending...")
            await send_fn(part_name)   # send immediately — don't wait for all parts
            # part file deleted inside send_fn after upload

    # Delete original large file
    try:
        fp.unlink()
    except Exception:
        pass

    await edit(f"✅ Sent all {total_parts} parts of **{fp.name}**")
    logger.info(f"/tmp after split+send: {get_tmp_usage()}")

def get_tmp_usage() -> str:
    try:
        stat = shutil.disk_usage("/tmp")
        return f"{human_size(stat.total - stat.free)} / {human_size(stat.total)}"
    except Exception:
        return "unknown"

def get_tmp_free_bytes() -> int:
    try:
        return shutil.disk_usage("/tmp").free
    except Exception:
        return 0

def cleanup_stale_tmp(min_free_bytes: int = 500 * 1024 * 1024):
    """Delete old bot tmp dirs if free space is below min_free_bytes (default 500 MB)."""
    if get_tmp_free_bytes() >= min_free_bytes:
        return
    logger.warning(f"/tmp low on space ({get_tmp_usage()}), cleaning stale dirs...")
    try:
        for entry in sorted(Path("/tmp").iterdir(), key=lambda p: p.stat().st_mtime):
            if entry.is_dir() and entry.name.startswith("tmp"):
                try:
                    shutil.rmtree(entry, ignore_errors=True)
                    logger.info(f"Cleaned stale dir: {entry}")
                except Exception:
                    pass
                if get_tmp_free_bytes() >= min_free_bytes:
                    break
    except Exception as e:
        logger.warning(f"cleanup_stale_tmp failed: {e}")

def auto_restart():
    """Restart the bot process with same arguments."""
    logger.info("Restarting bot...")
    os.execv(sys.executable, [sys.executable] + sys.argv)


def start_health_server():
    """Flask health server in a background daemon thread."""
    from flask import Flask
    flask_app = Flask(__name__)

    @flask_app.route("/")
    def home():
        s = load_settings()
        return f"Bot running | Engine: {s['library']} | Workers: {s['workers']}", 200

    @flask_app.route("/health")
    def health():
        return "OK", 200

    port = int(os.environ.get("PORT", 8080))
    import threading
    t = threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=port, use_reloader=False),
        daemon=True,
    )
    t.start()
    logger.info(f"Flask health server started on port {port}")

async def run_health_server():
    """Async wrapper — starts Flask in background thread (non-blocking)."""
    start_health_server()

def check_cmd(name):
    try:
        subprocess.run([name, "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False

def settings_text() -> str:
    s   = load_settings()
    lib = s["library"]
    return (
        f"⚙️ **Bot Settings**\n\n"
        f"📦 **Library:** `{lib}`\n"
        f"👷 **Workers:** `{s['workers']}`\n"
        f"💾 **Max Download:** `{s['max_dl_gb']} GB`\n\n"
        f"**Change settings:**\n"
        f"`/set library pyrogram` — Switch to Pyrogram\n"
        f"`/set library telethon` — Switch to Telethon\n"
        f"`/set workers 4` — Upload workers (1–8)\n"
        f"`/set maxdl 1.5` — Max download size in GB\n`/set text Team Secret` — Floating watermark (empty to disable)\n\n"
        f"⚠️ Restart bot after changing library/workers."
    )


# ── Download handlers (library-agnostic) ─────────────────────────────────────

async def handle_gdrive_file(send_fn, edit, file_id, tmp_dir):
    cleanup_stale_tmp()
    logger.info(f"[GDRIVE] Download start: file_id={file_id}")
    await edit("⬇️ Downloading from Google Drive...")
    loop      = asyncio.get_running_loop()
    real_name = await loop.run_in_executor(None, lambda: get_real_filename(file_id))
    logger.info(f"[GDRIVE] Resolved filename: {real_name}")
    downloaded = await loop.run_in_executor(
        None, lambda: gdown.download(
            f"https://drive.google.com/uc?id={file_id}&export=download",
            output=tmp_dir + "/", quiet=False, fuzzy=True
        )
    )
    if get_cancel_event().is_set(): raise asyncio.CancelledError()
    if not downloaded or not os.path.exists(downloaded):
        raise Exception("Download failed. File may be private.")
    fp = Path(downloaded)
    logger.info(f"[GDRIVE] Downloaded: {fp.name} size={human_size(fp.stat().st_size)}")
    if fp.name == file_id or "." not in fp.name:
        if real_name:
            new = fp.parent / real_name; fp.rename(new); fp = new
    fp = fix_filename(fp)
    await split_and_send(send_fn, edit, fp)


async def handle_gdrive_folder(send_fn, edit, folder_id, tmp_dir):
    cleanup_stale_tmp()
    folder_dir = os.path.join(tmp_dir, "folder")
    os.makedirs(folder_dir, exist_ok=True)
    loop = asyncio.get_running_loop()
    await edit("⬇️ Fetching Google Drive folder...")
    await loop.run_in_executor(
        None, lambda: gdown.download_folder(
            f"https://drive.google.com/drive/folders/{folder_id}",
            output=folder_dir, quiet=True, remaining_ok=True
        )
    )
    all_files = sorted([f for f in Path(folder_dir).rglob("*") if f.is_file()], key=lambda f: f.name.lower())
    if not all_files:
        raise Exception("No files found or folder is private.")
    await edit(f"📦 {len(all_files)} file(s) found. Sending...")
    for i, fp in enumerate(all_files, 1):
        fp = fix_filename(fp)
        await edit(f"📤 {i}/{len(all_files)}: **{fp.name}** ({human_size(fp.stat().st_size)})")
        await send_fn(fp)



def aria2c_download(url: str, dest_path: str, extra_headers: dict | None = None,
                    connections: int = 16, progress_cb=None, cancel_flag=None) -> None:
    """Download a file using aria2c with multi-connection for maximum speed.
    Falls back to single-connection requests if aria2c fails."""
    import shutil
    out_dir  = str(Path(dest_path).parent)
    out_file = Path(dest_path).name
    cmd = [
        "aria2c",
        "--split=" + str(connections),
        "--max-connection-per-server=" + str(connections),
        "--min-split-size=1M",
        "--file-allocation=none",
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--console-log-level=error",
        "--summary-interval=3",
        "-d", out_dir,
        "-o", out_file,
    ]
    if extra_headers:
        for k, v in extra_headers.items():
            cmd += ["--header", f"{k}: {v}"]
    cmd.append(url)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for line in proc.stdout:
            if cancel_flag and cancel_flag[0]:
                proc.terminate()
                raise asyncio.CancelledError()
            line = line.strip()
            if line and progress_cb:
                progress_cb(line)
        proc.wait()
    except Exception:
        proc.terminate()
        raise
    if proc.returncode != 0:
        raise Exception(f"aria2c failed with code {proc.returncode}")

async def handle_direct(send_fn, edit, url, tmp_dir, disk_only: bool = False,
                        extra_headers: dict | None = None):
    """Stream directly from URL to Telegram (Pyrogram).
    disk_only=True: always download to disk first (Telethon — avoids double /tmp copy).
    Falls back to disk for files that need splitting (> 2 GB).
    extra_headers: additional HTTP headers (e.g. Referer) forwarded to all requests."""
    import io
    loop     = asyncio.get_running_loop()
    cancel   = get_cancel_event()

    # Merge extra_headers into a session-like headers dict for this download
    req_headers: dict = {}
    if extra_headers:
        req_headers.update(extra_headers)

    def http_get(u, **kw):
        h = dict(req_headers)
        h.update(kw.pop("headers", {}))
        return HTTP.get(u, headers=h, **kw)

    def http_head(u, **kw):
        h = dict(req_headers)
        h.update(kw.pop("headers", {}))
        return HTTP.head(u, headers=h, **kw)

    await edit("🔍 Checking file info...")
    def _get_size():
        try:
            r = http_head(url, allow_redirects=True, timeout=10)
            return int(r.headers.get("Content-Length", 0))
        except Exception:
            return 0
    remote_size = await loop.run_in_executor(None, _get_size)
    if remote_size > MAX_DL_SIZE:
        raise Exception(f"File too large: {human_size(remote_size)} (max {human_size(MAX_DL_SIZE)})")

    filename = await loop.run_in_executor(None, lambda: get_direct_filename(url))
    ext      = Path(filename).suffix.lower()

    size_str = f" ({human_size(remote_size)})" if remote_size else ""

    # ── Telethon disk_only path: download → disk → upload (no double copy) ───
    if disk_only:
        tmp_free = get_tmp_free_bytes()
        if remote_size > 0 and remote_size > tmp_free:
            raise Exception(f"Not enough /tmp space. Need {human_size(remote_size)}, free {human_size(tmp_free)}")
        dest_path = os.path.join(tmp_dir, filename)
        await edit(f"⬇️ **{filename}**{size_str}")
        _donly_last = [0.0]
        _donly_loop = loop
        async def _update_donly(downloaded: int):
            now = time.time()
            if now - _donly_last[0] < 3: return
            _donly_last[0] = now
            try:
                if remote_size:
                    await edit(f"⬇️ **{filename}**\n{progress_bar(downloaded, remote_size)}")
                else:
                    await edit(f"⬇️ **{filename}**\n📥 {human_size(downloaded)}")
            except Exception: pass
        cancelled = [False]
        MAX_RETRIES = 20
        MAX_CONSECUTIVE = 5
        def _dl_donly():
            downloaded = 0
            server_supports_range = True
            total_attempts = 0
            consecutive_errors = 0
            while total_attempts < MAX_RETRIES:
                total_attempts += 1
                try:
                    range_headers = {}
                    if downloaded > 0 and server_supports_range:
                        range_headers["Range"] = f"bytes={downloaded}-"
                        logger.info(f"[DIRECT/disk] Resuming from {human_size(downloaded)} "
                                    f"(attempt {total_attempts}/{MAX_RETRIES}, "
                                    f"consecutive errors: {consecutive_errors})")
                    with http_get(url, stream=True, timeout=(10, 60),
                                  allow_redirects=True, headers=range_headers) as r:
                        # 416 = server doesn't support Range → discard partial, restart from 0
                        if r.status_code == 416:
                            logger.warning("[DIRECT/disk] Range not supported, restarting from 0")
                            server_supports_range = False
                            downloaded = 0
                            if os.path.exists(dest_path):
                                os.remove(dest_path)
                            with http_get(url, stream=True, timeout=(10, 60), allow_redirects=True) as r2:
                                r2.raise_for_status()
                                with open(dest_path, "wb") as f:
                                    for chunk in r2.iter_content(chunk_size=1024 * 1024):
                                        if cancel.is_set():
                                            cancelled[0] = True
                                            return
                                        if chunk:
                                            f.write(chunk)
                                            downloaded += len(chunk)
                                            consecutive_errors = 0  # progress → reset streak
                                            asyncio.run_coroutine_threadsafe(_update_donly(downloaded), _donly_loop)
                            logger.info(f"[DIRECT/disk] Done: {filename} {human_size(downloaded)}")
                            return
                        # Server returned 200 instead of 206 → ignored Range, restart from 0
                        if downloaded > 0 and r.status_code == 200:
                            logger.warning("[DIRECT/disk] Server ignored Range header, restarting from 0")
                            server_supports_range = False
                            downloaded = 0
                        r.raise_for_status()
                        mode = "ab" if downloaded > 0 else "wb"
                        with open(dest_path, mode) as f:
                            for chunk in r.iter_content(chunk_size=1024 * 1024):
                                if cancel.is_set():
                                    cancelled[0] = True
                                    return
                                if chunk:
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    consecutive_errors = 0  # progress → reset streak
                                    asyncio.run_coroutine_threadsafe(_update_donly(downloaded), _donly_loop)
                    logger.info(f"[DIRECT/disk] Done: {filename} {human_size(downloaded)}")
                    return  # success
                except (requests.exceptions.ChunkedEncodingError,
                        requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout) as e:
                    consecutive_errors += 1
                    if consecutive_errors >= MAX_CONSECUTIVE:
                        raise Exception(
                            f"Gave up after {consecutive_errors} consecutive errors "
                            f"at {human_size(downloaded)}. Last: {e}"
                        )
                    if total_attempts >= MAX_RETRIES:
                        raise
                    wait = min(2 ** consecutive_errors, 30)  # backoff on streak, capped 30s
                    logger.warning(f"[DIRECT/disk] Interrupted at {human_size(downloaded)}, "
                                   f"retry in {wait}s "
                                   f"(attempt {total_attempts}/{MAX_RETRIES}, "
                                   f"consecutive: {consecutive_errors}/{MAX_CONSECUTIVE}): {e}")
                    time.sleep(wait)
        try:
            await loop.run_in_executor(None, _dl_donly)
        except Exception:
            if os.path.exists(dest_path):
                os.remove(dest_path)
            raise
        if cancelled[0]: raise asyncio.CancelledError()
        fp = fix_filename(Path(dest_path))
        if not fp.exists(): raise Exception("Download failed.")
        await split_and_send(send_fn, edit, fp)
        return

    # ── files ≤ 2 GB: stream into memory pipe, upload without touching disk ──
    if remote_size <= MAX_FILE_SIZE and not disk_only:

        class StreamingReader(io.RawIOBase):
            """Wraps requests streaming response as a readable file-like object."""
            def __init__(self):
                self._resp  = http_get(url, stream=True, timeout=(10, 300), allow_redirects=True)
                self._resp.raise_for_status()
                self._iter  = self._resp.iter_content(chunk_size=512 * 1024)
                self._buf   = b""
                self.uploaded = 0

            def readable(self):
                return True

            def readinto(self, b):
                if cancel.is_set():
                    return 0          # signals EOF → Pyrogram/Telethon will stop
                MAX_RETRIES = 5
                for attempt in range(MAX_RETRIES):
                    try:
                        while not self._buf:
                            try:
                                self._buf = next(self._iter)
                            except StopIteration:
                                return 0      # EOF
                        break  # got data
                    except (requests.exceptions.ChunkedEncodingError,
                            requests.exceptions.ConnectionError,
                            requests.exceptions.Timeout) as e:
                        if attempt >= MAX_RETRIES - 1:
                            raise
                        wait = min(2 ** (attempt + 1), 30)
                        logger.warning(f"[DIRECT/stream] Broken at {human_size(self.uploaded)}, "
                                       f"reconnecting in {wait}s (attempt {attempt+1}/{MAX_RETRIES}): {e}")
                        time.sleep(wait)
                        try: self._resp.close()
                        except Exception: pass
                        headers = {"Range": f"bytes={self.uploaded}-"}
                        self._resp = http_get(url, stream=True, timeout=(10, 300),
                                              allow_redirects=True, headers=headers)
                        if self._resp.status_code not in (200, 206):
                            self._resp = http_get(url, stream=True, timeout=(10, 300), allow_redirects=True)
                        self._iter = self._resp.iter_content(chunk_size=512 * 1024)
                        self._buf = b""
                n = min(len(b), len(self._buf))
                b[:n] = self._buf[:n]
                self._buf = self._buf[n:]
                self.uploaded += n
                return n

            def close(self):
                try: self._resp.close()
                except Exception: pass
                super().close()

        logger.info(f"[DIRECT] Stream start: {filename} size={human_size(remote_size) if remote_size else '?'} url={url}")

        _dl_last_edit = [0.0]

        async def _update_dl_progress(transferred: int):
            """Edit status message with download progress (max once per 3s)."""
            now = time.time()
            if now - _dl_last_edit[0] < 3:
                return
            _dl_last_edit[0] = now
            try:
                if remote_size:
                    line = progress_bar(transferred, remote_size)
                    await edit(f"⬇️ **{filename}**\n{line}")
                else:
                    await edit(f"⬇️ **{filename}**\n📥 {human_size(transferred)}")
            except Exception:
                pass

        class LoggingReader(io.RawIOBase):
            """Wraps requests streaming response with Telegram progress updates."""
            def __init__(self):
                self._resp     = http_get(url, stream=True, timeout=(10, 300), allow_redirects=True)
                self._resp.raise_for_status()
                self._iter     = self._resp.iter_content(chunk_size=512 * 1024)
                self._buf      = b""
                self.uploaded  = 0
                self._loop     = asyncio.get_event_loop()

            def readable(self): return True

            def readinto(self, b):
                if cancel.is_set():
                    return 0
                MAX_RETRIES = 5
                for attempt in range(MAX_RETRIES):
                    try:
                        while not self._buf:
                            try:
                                self._buf = next(self._iter)
                            except StopIteration:
                                logger.info(f"[DIRECT] Stream EOF: {filename} total={human_size(self.uploaded)}")
                                return 0
                        break  # got data
                    except (requests.exceptions.ChunkedEncodingError,
                            requests.exceptions.ConnectionError,
                            requests.exceptions.Timeout) as e:
                        if attempt >= MAX_RETRIES - 1:
                            raise
                        wait = min(2 ** (attempt + 1), 30)
                        logger.warning(f"[DIRECT] Stream broken at {human_size(self.uploaded)}, "
                                       f"reconnecting in {wait}s (attempt {attempt+1}/{MAX_RETRIES}): {e}")
                        time.sleep(wait)
                        try: self._resp.close()
                        except Exception: pass
                        headers = {"Range": f"bytes={self.uploaded}-"}
                        self._resp = http_get(url, stream=True, timeout=(10, 300),
                                              allow_redirects=True, headers=headers)
                        if self._resp.status_code not in (200, 206):
                            self._resp = http_get(url, stream=True, timeout=(10, 300), allow_redirects=True)
                        self._iter = self._resp.iter_content(chunk_size=512 * 1024)
                        self._buf = b""
                n = min(len(b), len(self._buf))
                b[:n] = self._buf[:n]
                self._buf = self._buf[n:]
                self.uploaded += n
                asyncio.run_coroutine_threadsafe(_update_dl_progress(self.uploaded), self._loop)
                return n

            def close(self):
                try: self._resp.close()
                except Exception: pass
                super().close()

        class NamedBufferedReader(io.BufferedReader):
            """BufferedReader with a writable .name property (io.BufferedReader.name is read-only)."""
            @property
            def name(self):
                return self._name
            def __init__(self, raw, buffer_size, name):
                super().__init__(raw, buffer_size=buffer_size)
                self._name = name

        reader = LoggingReader()
        bio    = NamedBufferedReader(reader, buffer_size=4 * 1024 * 1024, name=filename)

        await send_fn(bio, filename=filename, file_size=remote_size if remote_size else None)
        logger.info(f"[DIRECT] Stream upload done: {filename}")

        if cancel.is_set():
            raise asyncio.CancelledError()
        return

    # ── files > 2 GB: must save to disk first then split ──────────────────────
    cleanup_stale_tmp()
    tmp_free = get_tmp_free_bytes()
    if remote_size > 0 and remote_size > tmp_free:
        raise Exception(f"Not enough /tmp space. Need {human_size(remote_size)}, free {human_size(tmp_free)}")

    dest_path  = os.path.join(tmp_dir, filename)
    await edit(f"⬇️ Downloading **{filename}** to disk (>{human_size(MAX_FILE_SIZE)}, will split)...")

    logger.info(f"[DIRECT] Disk download start: {filename} size={human_size(remote_size) if remote_size else '?'}")
    cancelled  = [False]
    _disk_last = [0.0]

    async def _update_disk_progress(downloaded: int):
        now = time.time()
        if now - _disk_last[0] < 3:
            return
        _disk_last[0] = now
        try:
            if remote_size:
                line = progress_bar(downloaded, remote_size)
                await edit(f"⬇️ **{filename}**\n{line}")
            else:
                await edit(f"⬇️ **{filename}**\n📥 {human_size(downloaded)}")
        except Exception:
            pass

    _disk_loop = asyncio.get_running_loop()
    def _dl():
        downloaded = 0
        server_supports_range = True
        MAX_RETRIES      = 20
        MAX_CONSECUTIVE  = 5
        total_attempts   = 0
        consecutive_errors = 0
        while total_attempts < MAX_RETRIES:
            total_attempts += 1
            try:
                range_headers = {}
                if downloaded > 0 and server_supports_range:
                    range_headers["Range"] = f"bytes={downloaded}-"
                    logger.info(f"[DIRECT/disk>2G] Resuming from {human_size(downloaded)} "
                                f"(attempt {total_attempts}/{MAX_RETRIES}, "
                                f"consecutive errors: {consecutive_errors})")
                with http_get(url, stream=True, timeout=(10, 300),
                              allow_redirects=True, headers=range_headers) as r:
                    if r.status_code == 416:
                        logger.warning("[DIRECT/disk>2G] Range not supported, restarting from 0")
                        server_supports_range = False
                        downloaded = 0
                        if os.path.exists(dest_path):
                            os.remove(dest_path)
                        with http_get(url, stream=True, timeout=(10, 300), allow_redirects=True) as r2:
                            r2.raise_for_status()
                            with open(dest_path, "wb") as f:
                                for chunk in r2.iter_content(chunk_size=4 * 1024 * 1024):
                                    if cancel.is_set():
                                        cancelled[0] = True
                                        return
                                    if chunk:
                                        f.write(chunk)
                                        downloaded += len(chunk)
                                        consecutive_errors = 0
                                        asyncio.run_coroutine_threadsafe(_update_disk_progress(downloaded), _disk_loop)
                        logger.info(f"[DIRECT/disk>2G] Done: {filename} {human_size(downloaded)}")
                        return
                    if downloaded > 0 and r.status_code == 200:
                        logger.warning("[DIRECT/disk>2G] Server ignored Range, restarting from 0")
                        server_supports_range = False
                        downloaded = 0
                    r.raise_for_status()
                    mode = "ab" if downloaded > 0 else "wb"
                    with open(dest_path, mode) as f:
                        for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                            if cancel.is_set():
                                cancelled[0] = True
                                return
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                consecutive_errors = 0
                                asyncio.run_coroutine_threadsafe(_update_disk_progress(downloaded), _disk_loop)
                logger.info(f"[DIRECT/disk>2G] Done: {filename} total={human_size(downloaded)}")
                return  # success
            except (requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                consecutive_errors += 1
                if consecutive_errors >= MAX_CONSECUTIVE:
                    raise Exception(
                        f"Gave up after {consecutive_errors} consecutive errors "
                        f"at {human_size(downloaded)}. Last: {e}"
                    )
                if total_attempts >= MAX_RETRIES:
                    raise
                wait = min(2 ** consecutive_errors, 30)
                logger.warning(f"[DIRECT/disk>2G] Interrupted at {human_size(downloaded)}, "
                               f"retry in {wait}s "
                               f"(attempt {total_attempts}/{MAX_RETRIES}, "
                               f"consecutive: {consecutive_errors}/{MAX_CONSECUTIVE}): {e}")
                time.sleep(wait)
    await loop.run_in_executor(None, _dl)
    if cancelled[0]:
        raise asyncio.CancelledError()
    fp = fix_filename(Path(dest_path))
    if not fp.exists():
        raise Exception("Download failed.")
    await split_and_send(send_fn, edit, fp)


async def handle_ytdlp(send_fn, edit, url, tmp_dir, extra_args: list | None = None):
    if not check_cmd("yt-dlp"):
        raise Exception("yt-dlp not installed. Run: pip install yt-dlp")
    cleanup_stale_tmp()
    logger.info(f"[YTDLP] Download start: {url}")
    await edit("🔍 Fetching media info...")
    loop = asyncio.get_running_loop()
    is_stream    = any(urllib.parse.urlparse(url).path.lower().endswith(e) for e in STREAM_EXTS)
    is_instagram = "instagram.com" in url or "instagr.am" in url
    outtmpl      = os.path.join(tmp_dir, "stream_%(id)s.%(ext)s" if is_stream else "%(title).60s.%(ext)s")

    # Format priority:
    # 1. Best mp4 video + best m4a audio (merged)              — ideal
    # 2. Best mp4 video + any best audio                       — common Instagram fallback
    # 3. Best video + best m4a audio                           — cross-format merge
    # 4. Best video + any audio                                — generic merge
    # 5. Pre-muxed file with both streams                      — Reels/small clips
    # 6. Best file with audio codec                            — last resort with audio
    # 7. Absolute best                                         — no audio filter
    # NOTE: avoid filesize filters — Instagram/TikTok don't report sizes in manifests.
    fmt = (
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[ext=mp4]+bestaudio/"
        "bestvideo+bestaudio[ext=m4a]/"
        "bestvideo+bestaudio/"
        "best[acodec!=none][vcodec!=none]/"
        "best[acodec!=none]/"
        "best"
    )
    cmd = [
        "yt-dlp", "--no-playlist",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "--max-filesize", str(MAX_DL_SIZE),
        "--output", outtmpl, "--no-warnings", "--hls-prefer-ffmpeg",
    ]
    if is_instagram:
        # Instagram small Reels often come as a single muxed stream —
        # using a mobile UA makes the API return proper muxed mp4 with audio.
        cmd += [
            "--add-header",
            "User-Agent:Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        ]
    if extra_args:
        cmd += extra_args
    cmd.append(url)
    await edit("⬇️ Downloading stream..." if is_stream else "⬇️ Downloading via yt-dlp...")
    cancel = get_cancel_event()
    proc_holder = [None]

    def _run():
        proc_holder[0] = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        _, stderr = proc_holder[0].communicate()
        if cancel.is_set():
            return
        if proc_holder[0].returncode != 0:
            raise Exception(stderr.strip() or "yt-dlp failed.")

    fut = loop.run_in_executor(None, _run)
    while not fut.done():
        if cancel.is_set():
            try:
                if proc_holder[0]: proc_holder[0].terminate()
            except Exception: pass
            raise asyncio.CancelledError()
        await asyncio.sleep(1)
    fut.result()  # re-raise any exception from _run
    if cancel.is_set(): raise asyncio.CancelledError()
    files = [f for f in Path(tmp_dir).iterdir() if f.is_file()]
    if not files: raise Exception("yt-dlp: no output file created.")
    for fp in sorted(files, key=lambda f: f.stat().st_size, reverse=True):
        logger.info(f"[YTDLP] Sending: {fp.name} size={human_size(fp.stat().st_size)}")
        await split_and_send(send_fn, edit, fp)


async def handle_magnet(send_fn, edit, magnet, tmp_dir):
    if not check_cmd("aria2c"):
        raise Exception("aria2c not installed. Run: apt install aria2")
    cleanup_stale_tmp()
    await edit("🧲 Magnet download shuru ho raha hai...")

    RPC_PORT = 6800
    RPC_URL  = f"http://localhost:{RPC_PORT}/jsonrpc"
    RPC_SECRET = "aria2secret"

    # ── aria2c daemon RPC mode mein start karo ────────────────────────────────
    daemon_cmd = [
        "aria2c",
        "--enable-rpc",
        f"--rpc-listen-port={RPC_PORT}",
        f"--rpc-secret={RPC_SECRET}",
        "--daemon=true",
        "--dir", tmp_dir,
        "--seed-time=0",
        "--max-connection-per-server=4",
        "--split=4",
        "--bt-stop-timeout=300",
        "--log-level=error",
    ]
    proc = await asyncio.create_subprocess_exec(
        *daemon_cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await asyncio.sleep(2)  # daemon start hone do

    loop = asyncio.get_running_loop()

    def rpc(method, params=None):
        payload = {
            "jsonrpc": "2.0", "id": "bot",
            "method": method,
            "params": [f"token:{RPC_SECRET}"] + (params or []),
        }
        r = HTTP.post(RPC_URL, json=payload, timeout=10)
        return r.json().get("result")

    # ── Magnet add karo ───────────────────────────────────────────────────────
    try:
        gid = await loop.run_in_executor(None, lambda: rpc("aria2.addUri", [[magnet]]))
    except Exception as e:
        raise Exception(f"aria2c RPC error: {e}. Check if port {RPC_PORT} is free.")

    await edit(f"🧲 Magnet queued! Peers dhundh raha hai...\n🔑 GID: `{gid}`")

    # ── Progress polling ──────────────────────────────────────────────────────
    start_time = loop.time()
    TIMEOUT    = 1800  # 30 min

    while True:
        await asyncio.sleep(3)

        if loop.time() - start_time > TIMEOUT:
            await loop.run_in_executor(None, lambda: rpc("aria2.remove", [gid]))
            raise Exception("Magnet timed out (30 min). Try with more seeders.")

        try:
            status = await loop.run_in_executor(None, lambda: rpc("aria2.tellStatus", [gid]))
        except Exception:
            continue

        if not status:
            continue

        dl_state  = status.get("status", "")
        completed = int(status.get("completedLength", 0))
        total     = int(status.get("totalLength", 0))
        speed     = int(status.get("downloadSpeed", 0))
        seeders   = status.get("numSeeders", "0")
        name      = status.get("bittorrent", {}).get("info", {}).get("name", "Unknown")

        if dl_state == "error":
            err = status.get("errorMessage", "Unknown error")
            raise Exception(f"aria2c error: {err}")

        if dl_state == "complete":
            await edit(f"✅ Download complete!\n📁 **{name}**\n📦 {human_size(total)}")
            break

        pct = (completed * 100 // total) if total > 0 else 0
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        eta_sec = ((total - completed) // speed) if speed > 0 else 0
        eta_str = f"{eta_sec // 60}m {eta_sec % 60}s" if speed > 0 else "..."

        state_icon = {
            "active":  "⬇️",
            "waiting": "⏳",
            "paused":  "⏸",
        }.get(dl_state, "🔄")

        try:
            await edit(
                f"🧲 **{name or 'Magnet Download'}**\n"
                f"{bar} {pct}%\n"
                f"{state_icon} {human_size(completed)} / {human_size(total) if total else '?'}\n"
                f"⚡ Speed: {human_size(speed)}/s\n"
                f"🌱 Seeders: {seeders}\n"
                f"⏱ ETA: {eta_str}"
            )
        except Exception:
            pass

    # ── Aria2c daemon band karo ───────────────────────────────────────────────
    try:
        await loop.run_in_executor(None, lambda: rpc("aria2.shutdown"))
    except Exception:
        pass

    # ── Files bhejo ──────────────────────────────────────────────────────────
    files = [f for f in Path(tmp_dir).rglob("*") if f.is_file()]
    if not files:
        raise Exception("No files downloaded from magnet.")

    logger.info(f"[MAGNET] Download complete. {len(files)} file(s) found.")
    await edit(f"📦 {len(files)} file(s) mili. Bhej raha hoon...")
    for i, fp in enumerate(sorted(files, key=lambda f: f.name.lower()), 1):
        logger.info(f"[MAGNET] Sending {i}/{len(files)}: {fp.name} size={human_size(fp.stat().st_size)}")
        await edit(f"📤 {i}/{len(files)}: **{fp.name}** ({human_size(fp.stat().st_size)})")
        await split_and_send(send_fn, edit, fp)


# ── Common set command logic ──────────────────────────────────────────────────

async def handle_set_cmd(parts, reply_fn):
    if len(parts) < 3:
        await reply_fn("Usage: `/set library pyrogram|telethon` | `/set workers 4` | `/set maxdl 1.5`")
        return
    key, val = parts[1].lower(), parts[2].lower()
    s = load_settings()

    if key == "library":
        if val not in ("pyrogram", "telethon"):
            await reply_fn("❌ Library must be `pyrogram` or `telethon`"); return
        if s["library"] == val:
            await reply_fn(f"ℹ️ Already using `{val}`"); return
        s["library"] = val; save_settings(s)
        await reply_fn(f"✅ Library switched to `{val}`\n🔄 Restarting bot...")
        await asyncio.sleep(1)   # reply bhejne ka waqt do
        auto_restart()

    elif key == "workers":
        try:
            n = int(val); assert 1 <= n <= 8
        except Exception:
            await reply_fn("❌ Workers must be 1–8"); return
        s["workers"] = n; save_settings(s)
        await reply_fn(f"✅ Workers set to `{n}`\n🔄 Restarting bot...")
        await asyncio.sleep(1)
        auto_restart()

    elif key == "maxdl":
        try:
            n = float(val); assert 0.1 <= n <= 10.0
        except Exception:
            await reply_fn("❌ Max download must be 0.1–10.0 GB"); return
        s["max_dl_gb"] = n; save_settings(s)
        await reply_fn(f"✅ Max download set to `{n} GB` (files > 2GB will be split)")

    elif key == "text":
        wm = " ".join(parts[2:]).strip()   # support multi-word text
        s["watermark"] = wm; save_settings(s)
        if wm:
            await reply_fn(f"✅ Watermark set to: `{wm}`\n📹 Will be applied to all videos.")
        else:
            await reply_fn("✅ Watermark disabled.")

    else:
        await reply_fn("❌ Unknown key. Use: `library`, `workers`, `maxdl`, `text`")


def get_video_meta(filepath: str) -> dict:
    """Extract width, height, duration, has_audio from video using ffprobe."""
    try:
        import json as _json
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", filepath],
            capture_output=True, text=True, timeout=15,
        )
        data    = _json.loads(result.stdout)
        streams = data.get("streams", [])
        vstream = next((s for s in streams if s.get("codec_type") == "video"), {})
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        width    = int(vstream.get("width") or 0)
        height   = int(vstream.get("height") or 0)
        dur_str  = vstream.get("duration") or "0"
        duration = int(float(dur_str)) if dur_str else 0
        return {
            "width":     max(0, width),
            "height":    max(0, height),
            "duration":  max(0, duration),
            "has_audio": has_audio,
        }
    except Exception:
        return {"width": 0, "height": 0, "duration": 0, "has_audio": False}

def ensure_audio_track(filepath: str) -> str:
    """If video has no audio stream, add a silent audio track via ffmpeg.
    Telegram converts silent short videos to GIF regardless of size — this prevents that.
    Returns path to fixed file (may be a new temp file), or original if ffmpeg unavailable."""
    try:
        meta = get_video_meta(filepath)
        if meta.get("has_audio", True):
            return filepath   # already has audio, nothing to do
        p        = Path(filepath)
        out_path = str(p.parent / (p.stem + "_audio" + p.suffix))
        result   = subprocess.run(
            [
                "ffmpeg", "-y", "-i", filepath,
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-c:v", "copy", "-c:a", "aac", "-shortest",
                "-movflags", "+faststart",
                out_path,
            ],
            capture_output=True, timeout=120,
        )
        if result.returncode == 0 and Path(out_path).exists():
            logger.info(f"[FFMPEG] Added silent audio track: {Path(filepath).name}")
            try: Path(filepath).unlink()
            except Exception: pass
            return out_path
    except Exception as e:
        logger.warning(f"[FFMPEG] ensure_audio_track failed: {e}")
    return filepath



def apply_watermark(filepath: str, text: str) -> str:
    """Add floating watermark text to video using ffmpeg."""
    if not text:
        return filepath
    try:
        p        = Path(filepath)
        out_path = str(p.parent / (p.stem + "_wm" + p.suffix))
        vf = (
            "drawtext=text='" + text + "'"
            ":fontsize=24:fontcolor=white@0.6:borderw=1:bordercolor=black@0.4"
            ":x='mod(t*60\\,W-text_w)':y='abs(sin(t*0.5))*(H-text_h)'"
        )
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", filepath, "-vf", vf,
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
             "-b:v", "0", "-maxrate", "2M", "-bufsize", "4M",
             "-c:a", "copy", "-movflags", "+faststart", out_path],
            capture_output=True, timeout=600,
        )
        if result.returncode == 0 and Path(out_path).exists():
            logger.info(f"[WATERMARK] Applied: {Path(filepath).name}")
            try: Path(filepath).unlink()
            except Exception: pass
            return out_path
        else:
            logger.warning(f"[WATERMARK] Failed: {result.stderr.decode()[-200:]}")
    except Exception as e:
        logger.warning(f"[WATERMARK] Error: {e}")
    return filepath

async def handle_stream_page(send_fn, edit, url, tmp_dir):
    """
    Scrape a webpage for embedded video URLs, then download the best one.

    Strategy (in order):
      1. yt-dlp --simulate on the PAGE URL (not scraped URLs)
      2. HTML/JS scrape — JS template literals filtered out, URLs validated
      3. HEAD-check each candidate to pick first reachable one
      4. Referer header passed to handle_direct to avoid 403s
    """
    cancel = get_cancel_event()
    loop   = asyncio.get_running_loop()

    PAGE_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Referer": url,
    }

    def is_js_template(u: str) -> bool:
        """Return True if URL contains unresolved JS template literal like ${...}."""
        return bool(re.search(r'\$\{[^}]+\}', u))

    def url_ext(u: str) -> str:
        """Get extension from URL path (ignores query string)."""
        return Path(urllib.parse.urlparse(u).path).suffix.lower()

    # ── Step 1: yt-dlp on the PAGE URL ───────────────────────────────────────
    if check_cmd("yt-dlp"):
        await edit("🔍 yt-dlp se try kar raha hoon...")
        try:
            sim = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ["yt-dlp", "--simulate", "--no-playlist",
                     "--no-warnings", "--quiet", url],   # <-- PAGE URL, not scraped
                    capture_output=True, text=True, timeout=30,
                )
            )
            if sim.returncode == 0:
                logger.info(f"[STREAM_PAGE] yt-dlp can handle page: {url}")
                await handle_ytdlp(send_fn, edit, url, tmp_dir)
                return
            logger.info(f"[STREAM_PAGE] yt-dlp can't handle page (rc={sim.returncode}), scraping...")
        except Exception as e:
            logger.warning(f"[STREAM_PAGE] yt-dlp simulate failed: {e}")

    if cancel.is_set():
        raise asyncio.CancelledError()

    # ── Step 2: fetch page HTML ───────────────────────────────────────────────
    await edit("🔍 Page scrape kar raha hoon...")
    try:
        resp = await loop.run_in_executor(
            None,
            lambda: HTTP.get(url, timeout=15, allow_redirects=True, headers=PAGE_HEADERS)
        )
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        raise Exception(f"Page fetch failed: {e}")

    if cancel.is_set():
        raise asyncio.CancelledError()

    # ── Step 3: extract + filter candidates ──────────────────────────────────
    base = f"{urllib.parse.urlparse(url).scheme}://{urllib.parse.urlparse(url).netloc}"

    def make_absolute(u: str) -> str:
        u = u.strip().strip('"\'')
        if u.startswith("//"):   return "https:" + u
        if u.startswith("/"):    return base + u
        if not u.startswith("http"): return url.rstrip("/") + "/" + u
        return u

    candidates: list[tuple[int, str]] = []   # (priority, url)  lower = better

    # Priority scheme:
    #   1 = HLS/DASH stream (.m3u8 / .mpd)
    #   2 = direct video with explicit key=value assignment in JS/HTML
    #   3 = direct video found as bare URL (less reliable)

    # Named assignment patterns: src=, file=, url=, stream=, hls=, ...
    named_re = re.compile(
        r'(?:src|file|url|source|stream|hls|dash|video|media|href)\s*[=:]\s*'
        r'["\']?(https?://[^\s"\'<>{}\[\]]+?'
        r'(?:\.m3u8|\.mpd|\.mp4|\.mkv|\.mov|\.webm|\.avi|\.flv|\.m4v|\.3gp|\.ts)'
        r'[^\s"\'<>{}\[\]]*)',
        re.IGNORECASE,
    )
    for m in named_re.finditer(html):
        u = make_absolute(m.group(1))
        if is_js_template(u): continue          # ← skip ${videoUrlStr} garbage
        ext = url_ext(u)
        pri = 1 if ext in STREAM_EXTS else 2
        candidates.append((pri, u))
        logger.debug(f"[STREAM_PAGE] named hit pri={pri}: {u[:100]}")

    # <video src> / <source src> HTML tags
    tag_re = re.compile(r'<(?:video|source)[^>]+src\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
    for m in tag_re.finditer(html):
        u = make_absolute(m.group(1))
        if is_js_template(u): continue
        candidates.append((2, u))
        logger.debug(f"[STREAM_PAGE] tag hit: {u[:100]}")

    # og:video meta
    og_re = re.compile(
        r'<meta[^>]+(?:property|name)\s*=\s*["\']og:video(?::url)?["\'][^>]+'
        r'content\s*=\s*["\']([^"\']+)["\']',
        re.IGNORECASE,
    )
    for m in og_re.finditer(html):
        u = make_absolute(m.group(1))
        if is_js_template(u): continue
        candidates.append((1, u))
        logger.debug(f"[STREAM_PAGE] og:video hit: {u[:100]}")

    # JSON-LD contentUrl / embedUrl
    jsonld_re = re.compile(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        re.DOTALL | re.IGNORECASE,
    )
    for m in jsonld_re.finditer(html):
        try:
            import json as _json
            obj = _json.loads(m.group(1))
            for key in ("contentUrl", "embedUrl", "url"):
                val = obj.get(key, "")
                if val and re.search(r'\.(m3u8|mp4|mpd|mkv|webm)', val, re.IGNORECASE):
                    u = make_absolute(val)
                    if not is_js_template(u):
                        candidates.append((1, u))
                        logger.debug(f"[STREAM_PAGE] json-ld {key}: {u[:100]}")
        except Exception:
            pass

    # Bare URLs in source (lowest priority fallback)
    bare_re = re.compile(
        r'https?://[^\s"\'<>{}\[\]]+?'
        r'(?:\.m3u8|\.mpd|\.mp4|\.mkv|\.mov|\.webm|\.avi|\.flv|\.m4v|\.3gp|\.ts)'
        r'(?:[^\s"\'<>{}\[\]]*)?',
        re.IGNORECASE,
    )
    for m in bare_re.finditer(html):
        u = m.group(0)
        if is_js_template(u): continue
        ext = url_ext(u)
        pri = 1 if ext in STREAM_EXTS else 3
        candidates.append((pri, u))

    # Deduplicate — keep best priority per URL
    seen: dict[str, int] = {}
    for pri, u in candidates:
        if u not in seen or pri < seen[u]:
            seen[u] = pri
    unique = sorted(seen.items(), key=lambda x: x[1])

    logger.info(f"[STREAM_PAGE] {len(unique)} unique candidate(s) after filtering")
    for u, p in unique[:8]:
        logger.info(f"[STREAM_PAGE]   pri={p} {u[:120]}")

    if not unique:
        raise Exception(
            "Page mein koi video URL nahi mili.\n"
            "Video login ke peeche, DRM-protected, ya JS se load ho rahi hai.\n"
            "Direct video URL (jaise .mp4 / .m3u8) copy karke bhejo."
        )

    if cancel.is_set():
        raise asyncio.CancelledError()

    # ── Step 4: HEAD-check each candidate, pick first reachable one ──────────
    await edit(f"🔎 {len(unique)} URL(s) mili — reachability check...")

    best_url: str | None = None
    best_ext: str        = ""

    def head_ok(u: str) -> bool:
        """Return True if server responds 2xx/3xx to HEAD with Referer."""
        try:
            r = HTTP.head(u, allow_redirects=True, timeout=8,
                          headers={"User-Agent": PAGE_HEADERS["User-Agent"],
                                   "Referer": url})
            logger.info(f"[STREAM_PAGE] HEAD {r.status_code} → {u[:100]}")
            return r.status_code < 400
        except Exception as e:
            logger.info(f"[STREAM_PAGE] HEAD error ({e}) → {u[:100]}")
            return False

    for u, _pri in unique:
        ok = await loop.run_in_executor(None, head_ok, u)
        if ok:
            best_url = u
            best_ext = url_ext(u)
            break

    if not best_url:
        # Nothing passed HEAD — just try the highest-priority URL anyway
        best_url, _ = unique[0]
        best_ext     = url_ext(best_url)
        logger.warning(f"[STREAM_PAGE] All HEAD checks failed, trying best candidate anyway: {best_url[:100]}")

    await edit(
        f"🎯 Video URL mili!\n"
        f"`{best_url[:90]}{'...' if len(best_url) > 90 else ''}`\n"
        f"⬇️ Download shuru..."
    )

    if cancel.is_set():
        raise asyncio.CancelledError()

    # ── Step 5: hand off with Referer header ─────────────────────────────────
    if best_ext in STREAM_EXTS or best_ext == ".ts":
        # HLS/DASH → yt-dlp (handles segmented streams, merges to mp4)
        await handle_ytdlp(send_fn, edit, best_url, tmp_dir,
                           extra_args=["--add-header", f"Referer:{url}"])
    else:
        # Direct media file — pass Referer so server doesn't 403
        await handle_direct(send_fn, edit, best_url, tmp_dir,
                            extra_headers={"Referer": url,
                                           "User-Agent": PAGE_HEADERS["User-Agent"]})


def extract_thumbnail(video_path: str, out_dir: str) -> str | None:
    """Extract frame from video as JPEG thumbnail using ffmpeg.
    Tries 5s, 2s, 1s, 10s timestamps to avoid black frames."""
    thumb_path = os.path.join(out_dir, "thumb.jpg")
    for ts in ["00:00:05", "00:00:02", "00:00:01", "00:00:10"]:
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-ss", ts,
                    "-i", video_path,
                    "-vframes", "1",
                    "-vf", "scale=320:-2",
                    "-q:v", "2",
                    thumb_path,
                ],
                capture_output=True, timeout=30,
            )
            if result.returncode == 0 and Path(thumb_path).exists() and Path(thumb_path).stat().st_size > 1000:
                logger.info(f"[THUMB] OK at {ts}: {thumb_path}")
                return thumb_path
        except Exception as e:
            logger.warning(f"[THUMB] Failed at {ts}: {e}")
    return None


async def handle_drm_download(send_fn_builder, edit_builder, reply_fn,
                               user_id: int, link: str, tmp_dir: str,
                               idx: int = 0, total: int = 0) -> bool:
    """
    Download a single link from the DRM txt list and send it with thumbnail.
    send_fn_builder / edit_builder are callables that accept a status_msg
    and return (send_fn, edit) — needed because we create a fresh status msg here.
    reply_fn: coroutine that sends a new message and returns it.
    idx / total: 1-based position in the batch (shown in caption).
    Returns True on success, False on failure/cancel.
    """
    if idx and total:
        remaining  = total - idx          # items still to go after this one
        index_tag  = f"[{idx}/{total}]"
        remain_tag = f"  ⏭ {remaining} remaining" if remaining > 0 else ""
        header     = f"{index_tag}{remain_tag}\n"
    else:
        index_tag  = ""
        header     = ""

    status = await reply_fn(f"⏳ {header}Downloading...")
    _raw_edit = edit_builder(status)

    # Wrap edit so every status update automatically shows index + remaining
    async def edit(text: str):
        try:
            await _raw_edit(f"{header}{text}" if header else text)
        except Exception:
            pass

    send_fn = send_fn_builder(status)

    lock = get_download_lock()
    if lock.locked():
        wait_msg = await reply_fn("⏳ Another download in progress. Queued — please wait...")
        await lock.acquire()
        try: await wait_msg.delete()
        except Exception: pass
    else:
        await lock.acquire()

    # NOTE: do NOT call cancel.clear() here — /cancel may have been triggered
    # between dispatching multiple DRM items; respect it.
    cancel = get_cancel_event()
    global _active_tmp_dir
    _active_tmp_dir = tmp_dir

    try:
        # Honour a cancel that arrived before we even started this item
        if cancel.is_set():
            await edit("🚫 Cancelled.")
            return False

        identifier, link_type = detect_link_type(link)
        if link_type == "unknown" or not identifier:
            await edit(f"❌ Invalid link: `{link[:80]}`")
            return False

        logger.info(f"[DRM] user={user_id} idx={idx}/{total} type={link_type} url={link[:80]}")

        # Wrapper send_fn: injects index tag into caption and attaches thumbnail
        async def send_with_thumb(fp, **kw):
            is_path = isinstance(fp, Path)
            fname   = kw.get("filename") or (fp.name if is_path else "file")
            ext     = Path(fname).suffix.lower()

            # Prepend index to caption so the user can track which file is which
            if idx and total:
                existing_caption = kw.get("caption", "")
                if existing_caption:
                    kw["caption"] = f"[{idx}/{total}] {existing_caption}"
                # (if no caption passed, pg_send/tl_send builds it; we'll inject below)
                kw["_drm_index"] = f"[{idx}/{total}]"   # picked up by send wrappers

            thumb = None
            if is_path and ext in VIDEO_EXTS:
                try:
                    thumb = await asyncio.get_running_loop().run_in_executor(
                        None, extract_thumbnail, str(fp), tmp_dir
                    )
                except Exception:
                    pass

            if thumb:
                kw["thumb"] = thumb

            try:
                await send_fn(fp, **kw)
            finally:
                if thumb:
                    try: Path(thumb).unlink()
                    except Exception: pass

        if   link_type == "direct":        await handle_direct(send_with_thumb, edit, identifier, tmp_dir, disk_only=True)
        elif link_type == "stream_page":   await handle_stream_page(send_with_thumb, edit, identifier, tmp_dir)
        elif link_type == "ytdlp":         await handle_ytdlp(send_with_thumb, edit, identifier, tmp_dir)
        elif link_type == "gdrive_file":   await handle_gdrive_file(send_with_thumb, edit, identifier, tmp_dir)
        elif link_type == "gdrive_folder": await handle_gdrive_folder(send_with_thumb, edit, identifier, tmp_dir)
        elif link_type == "magnet":        await handle_magnet(send_with_thumb, edit, identifier, tmp_dir)

        return True   # success

    except asyncio.CancelledError:
        await edit("🚫 Download cancelled.")
        return False
    except Exception as e:
        logger.error(f"[DRM] Error: {e}", exc_info=True)
        await edit(f"❌ **Error:** {e}")
        return False
    finally:
        _active_tmp_dir = None
        shutil.rmtree(tmp_dir, ignore_errors=True)
        try: lock.release()
        except RuntimeError: pass
        logger.info(f"[DRM] Done idx={idx} | /tmp: {get_tmp_usage()}")


# ═════════════════════════════════════════════════════════════════════════════
#  PYROGRAM BOT
# ═════════════════════════════════════════════════════════════════════════════

def run_pyrogram():
    import signal
    from pyrogram import Client, filters
    from pyrogram.types import Message

    async def main():
        workers = SETTINGS.get("workers", 4)

        # Client MUST be created inside the running event loop
        bot = Client(
            "gdrive_bot_pyrogram",
            api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
            workers=workers,
            max_concurrent_transmissions=workers,
        )

        # ── progress & send helpers ───────────────────────────────────────────

        _pg_prog_last: dict = {}
        async def pg_progress(current, total, status_msg, filename):
            if total == 0 or status_msg is None: return
            now = time.time()
            key = id(status_msg)
            if now - _pg_prog_last.get(key, 0.0) < 3:
                return
            _pg_prog_last[key] = now
            try:
                line = upload_bar(current, total)
                await status_msg.edit_text(f"📤 **{filename}**\n{line}")
            except Exception:
                pass

        async def pg_send(client, message, status_msg, fp, filename=None, file_size=None, thumb=None, **extra):
            # fp can be a Path (disk file) or a file-like object (streaming)
            is_path  = isinstance(fp, Path)
            fname    = filename or (fp.name if is_path else "file")
            ext      = Path(fname).suffix.lower()
            fsize    = fp.stat().st_size if is_path else (file_size or 0)
            index_prefix = extra.get("_drm_index", "")
            index_part   = f"{index_prefix} " if index_prefix else ""
            _wm_tag = load_settings().get("watermark", "")
            if _wm_tag:
                _stem = Path(fname).stem
                _ext  = Path(fname).suffix
                fname = f"{_stem} {_wm_tag}{_ext}"
            caption  = (f"{index_part}✅ **{fname}**\n📦 {human_size(fsize)}"
                        if fsize else f"{index_part}✅ **{fname}**")
            src      = str(fp) if is_path else fp
            kw = dict(
                chat_id=message.chat.id, file_name=fname, caption=caption,
                progress=pg_progress, progress_args=(status_msg, fname),
            )
            if ext in VIDEO_EXTS:
                # Pyrogram send_video internally calls seek() — fails on live HTTP streams.
                # Buffer the stream to a temp file first so we can pass a seekable file
                # with full metadata, preventing Telegram from rendering it as a GIF.
                _loop = asyncio.get_running_loop()
                if not is_path:
                    import tempfile as _tf
                    _tmp = _tf.NamedTemporaryFile(delete=False, suffix=ext, dir="/tmp")
                    try:
                        try: await status_msg.edit_text(f"⬇️ Buffering **{fname}** for upload...")
                        except Exception: pass
                        def _buf():
                            while True:
                                chunk = fp.read(4 * 1024 * 1024)
                                if not chunk: break
                                _tmp.write(chunk)
                            _tmp.flush()
                        await _loop.run_in_executor(None, _buf)
                        _tmp.close()
                        _disk = Path(_tmp.name)
                        # Add silent audio track if missing — prevents GIF conversion
                        _fixed = await _loop.run_in_executor(None, ensure_audio_track, str(_disk))
                        _disk  = Path(_fixed)

                        # Extract thumbnail
                        if not thumb:
                            thumb = await _loop.run_in_executor(None, extract_thumbnail, str(_disk), str(_disk.parent))
                            logger.info(f"[THUMB] pg_send stream thumb: {thumb}")
                        _meta = get_video_meta(str(_disk))
                        await client.send_video(
                            video=str(_disk), supports_streaming=True,
                            width=int(_meta.get("width") or 0),
                            height=int(_meta.get("height") or 0),
                            duration=int(_meta.get("duration") or 0),
                            thumb=thumb,
                            **kw,
                        )
                    finally:
                        try: Path(_tmp.name).unlink()
                        except Exception: pass
                        if thumb:
                            try: Path(thumb).unlink()
                            except Exception: pass
                else:
                    # Ensure video has an audio track — Telegram converts silent
                    # videos to GIF regardless of file size or metadata.
                    src = await _loop.run_in_executor(None, ensure_audio_track, src)

                    # Extract thumbnail if not provided
                    if not thumb:
                        thumb = await _loop.run_in_executor(None, extract_thumbnail, src, str(Path(src).parent))
                        logger.info(f"[THUMB] pg_send disk thumb: {thumb}")
                    meta = get_video_meta(src)
                    await client.send_video(
                        video=src, supports_streaming=True,
                        width=int(meta.get("width") or 0),
                        height=int(meta.get("height") or 0),
                        duration=int(meta.get("duration") or 0),
                        thumb=thumb,
                        **kw,
                    )
                    if thumb:
                        try: Path(thumb).unlink()
                        except Exception: pass
            elif ext in AUDIO_EXTS:
                await client.send_audio(audio=src, **kw)
            else:
                await client.send_document(document=src, **kw)
            if is_path:
                try: fp.unlink()
                except Exception: pass
            logger.info(f"Sent {fname} | /tmp: {get_tmp_usage()}")
            if status_msg:
                try: await status_msg.delete()
                except Exception: pass

        # ── handlers ─────────────────────────────────────────────────────────

        @bot.on_message(filters.command("start"))
        async def start(_, msg: Message):
            await msg.reply_text(
                f"👋 **Universal Downloader Bot**\n"
                f"🔧 Engine: `Pyrogram` | Workers: `{workers}`\n\n"
                f"✅ Google Drive • Direct links • YouTube/Instagram/TikTok/etc • Magnets\n"
                f"/help — usage | /settings — config"
            )

        @bot.on_message(filters.command("help"))
        async def help_cmd(_, msg: Message):
            await msg.reply_text(
                "📖 **Supported links:**\n\n"
                "• Google Drive (file/folder)\n"
                "• Direct HTTP/HTTPS file links\n"
                "• YouTube, Instagram, Twitter/X, TikTok + 1000 more (yt-dlp)\n"
                "• `.m3u8` / HLS streams\n"
                "• Magnet links (aria2c required)\n"
                "• 🆕 Stream pages — kisi bhi webpage ka link do,\n"
                "  bot automatically video URL dhundh ke download karega\n\n"
                "⚠️ Max 2 GB | One download at a time\n"
                "/cancel — stop current download"
            )

        @bot.on_message(filters.command("settings"))
        async def settings_cmd(_, msg: Message):
            await msg.reply_text(settings_text())

        @bot.on_message(filters.command("set"))
        async def set_cmd(_, msg: Message):
            parts = msg.text.strip().split()
            await handle_set_cmd(parts, msg.reply_text)

        @bot.on_message(filters.command("cancel"))
        async def cancel_cmd(_, msg: Message):
            global _drm_batch_active, _drm_stop_after_current
            lock = get_download_lock()
            if not lock.locked() and not _drm_batch_active:
                await msg.reply_text("ℹ️ No download is currently running.")
                return
            if _drm_batch_active:
                # DRM batch: let current item finish, then stop
                _drm_stop_after_current = True
                await msg.reply_text("🚫 Current download will finish, then batch will stop.")
            else:
                get_cancel_event().set()
                await msg.reply_text("🚫 Cancel signal sent. Download will stop shortly...")

        @bot.on_message(filters.command("drm"))
        async def drm_cmd(_, msg: Message):
            uid = msg.from_user.id if msg.from_user else 0
            drm_sessions[uid] = {"state": "awaiting_file", "links": []}
            await msg.reply_text(
                "📄 **DRM Link Downloader**\n\n"
                "Ek `.txt` file bhejo jisme links hon (ek line = ek link).\n"
                "Bot links read karke index dikhayega."
            )

        @bot.on_message(filters.document & ~filters.bot)
        async def drm_file_handler(_, msg: Message):
            uid = msg.from_user.id if msg.from_user else 0
            session = drm_sessions.get(uid)
            if not session or session.get("state") != "awaiting_file":
                return   # not in DRM flow, ignore

            fname = msg.document.file_name or ""
            if not fname.lower().endswith(".txt"):
                await msg.reply_text("❌ Sirf `.txt` file bhejo.")
                return

            await msg.reply_text("📥 File read kar raha hoon...")
            tmp = tempfile.mkdtemp(dir="/tmp")
            try:
                dl_path = os.path.join(tmp, fname)
                await msg.download(file_name=dl_path)
                text = Path(dl_path).read_text(encoding="utf-8", errors="ignore")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

            links = [l.strip() for l in text.splitlines() if l.strip() and l.strip().startswith("http")]
            if not links:
                await msg.reply_text("❌ File mein koi valid HTTP link nahi mila.")
                drm_sessions.pop(uid, None)
                return

            drm_sessions[uid] = {"state": "awaiting_index", "links": links}

            reply = (
                f"✅ **{len(links)} links mili!**\n\n"
                f"📥 Konsa index download karna hai? (1–{len(links)})\n"
                f"Single `5` → 5 se end tak sab\n"
                f"Multiple: `1 3 5` | Range: `2-5`\n"
                f"Cancel: `/cancel`"
            )
            await msg.reply_text(reply)

        @bot.on_message(filters.text & ~filters.bot & ~filters.command(["start", "help", "settings", "set", "cancel", "drm"]))
        async def handle_message(client, msg: Message):
            if not msg.text:
                return
            # Skip messages that arrived before bot started (queued while offline)
            if msg.date and msg.date.timestamp() < BOT_START_TIME:
                return
            text = msg.text.strip()
            uid  = msg.from_user.id if msg.from_user else 0

            # ── DRM index selection ──────────────────────────────────────────
            session = drm_sessions.get(uid)
            if session and session.get("state") == "awaiting_index":
                links   = session["links"]
                tokens  = text.split()
                indices = set()

                # Single number → download from that index to end
                if len(tokens) == 1 and tokens[0].isdigit():
                    start = int(tokens[0])
                    indices.update(range(start, len(links) + 1))
                else:
                    # Parse "1 3 5" and "2-5" style input
                    for token in tokens:
                        if "-" in token:
                            parts = token.split("-", 1)
                            try:
                                a, b = int(parts[0]), int(parts[1])
                                indices.update(range(a, b + 1))
                            except ValueError:
                                pass
                        else:
                            try: indices.add(int(token))
                            except ValueError: pass

                valid = sorted(i for i in indices if 1 <= i <= len(links))
                if not valid:
                    await msg.reply_text(
                        f"❌ Invalid index. 1 se {len(links)} ke beech number bhejo.\n"
                        f"Example: `1` ya `1 3` ya `2-5`"
                    )
                    return

                drm_sessions.pop(uid, None)   # clear session

                total_items = len(valid)
                success_count = 0
                fail_count    = 0

                # Reset flags before starting the batch
                get_cancel_event().clear()
                global _drm_batch_active, _drm_stop_after_current
                _drm_batch_active        = True
                _drm_stop_after_current  = False
                cancelled_mid_batch      = False
                try:
                 for idx in valid:
                    # Check if /cancel was sent after the previous item finished
                    if _drm_stop_after_current:
                        remaining = total_items - success_count - fail_count
                        await msg.reply_text(
                            f"🚫 **Batch stopped after item {idx-1}.**\n"
                            f"✅ Success: {success_count} | ❌ Failed: {fail_count} | ⏭ Skipped: {remaining}"
                        )
                        cancelled_mid_batch = True
                        break

                    link    = links[idx - 1]
                    tmp_dir = tempfile.mkdtemp(dir="/tmp")

                    def _make_send(s): return lambda fp, **kw: pg_send(client, msg, s, fp, **kw)
                    def _make_edit(s):
                        async def _edit(t): await s.edit_text(t)
                        return _edit

                    ok = await handle_drm_download(
                        _make_send, _make_edit,
                        lambda t: msg.reply_text(t),
                        uid, link, tmp_dir,
                        idx=idx, total=len(links),
                    )
                    if ok:
                        success_count += 1
                    else:
                        fail_count += 1

                 if not cancelled_mid_batch:
                  # Completion summary
                  await msg.reply_text(
                      f"🏁 **Batch complete!**\n\n"
                      f"✅ Success: `{success_count}`\n"
                      f"❌ Failed:  `{fail_count}`\n"
                      f"📦 Total:   `{total_items}`"
                  )
                finally:
                 _drm_batch_active       = False
                 _drm_stop_after_current = False
                return
            # ── end DRM ──────────────────────────────────────────────────────
            identifier, link_type = detect_link_type(text)
            if link_type == "unknown" or not identifier:
                await msg.reply_text("❓ Unsupported link. Use /help."); return
            lock = get_download_lock()
            if lock.locked():
                wait_msg = await msg.reply_text("⏳ Another download in progress. You are queued — please wait...")
                await lock.acquire()
                try: await wait_msg.delete()
                except Exception: pass
            else:
                await lock.acquire()

            logger.info(f"[REQ] user={msg.from_user.id if msg.from_user else '?'} type={link_type} id={identifier[:60]}")
            status  = await msg.reply_text("⏳ Processing...")
            tmp_dir = tempfile.mkdtemp(dir="/tmp")

            async def send_fn(fp, **kw): await pg_send(client, msg, status, fp, **kw)
            async def edit(t):
                try: await status.edit_text(t)
                except Exception: pass

            global _active_tmp_dir
            _active_tmp_dir = tmp_dir
            cancel = get_cancel_event()
            cancel.clear()
            try:
                if True:  # lock already acquired above
                    if   link_type == "gdrive_folder": await handle_gdrive_folder(send_fn, edit, identifier, tmp_dir)
                    elif link_type == "gdrive_file":   await handle_gdrive_file(send_fn, edit, identifier, tmp_dir)
                    elif link_type == "ytdlp":         await handle_ytdlp(send_fn, edit, identifier, tmp_dir)
                    elif link_type == "direct":        await handle_direct(send_fn, edit, identifier, tmp_dir, disk_only=True)
                    elif link_type == "magnet":        await handle_magnet(send_fn, edit, identifier, tmp_dir)
                    elif link_type == "stream_page":   await handle_stream_page(send_fn, edit, identifier, tmp_dir)
            except asyncio.CancelledError:
                try: await status.edit_text("🚫 Download cancelled.")
                except Exception: pass
            except Exception as e:
                logger.error(f"Error: {e}", exc_info=True)
                try: await status.edit_text(f"❌ **Error:** {e}")
                except Exception: pass
            finally:
                cancel.clear()
                _active_tmp_dir = None
                shutil.rmtree(tmp_dir, ignore_errors=True)
                try: lock.release()
                except RuntimeError: pass
                logger.info(f"/tmp after cleanup: {get_tmp_usage()}")

        # ── start bot ────────────────────────────────────────────────────────

        await bot.start()
        logger.info("Pyrogram bot started and listening...")

        if OWNER_ID:
            s   = load_settings()
            txt = (
                f"✅ **Bot Started!**\n\n"
                f"🔧 Engine: `Pyrogram`\n"
                f"👷 Workers: `{s['workers']}`\n"
                f"💾 Max DL: `{s['max_dl_gb']} GB`\n"
                f"✂️ Split: `1.95 GB` per part\n"
                f"{'✅' if check_cmd('yt-dlp') else '❌'} yt-dlp | "
                f"{'✅' if check_cmd('aria2c') else '❌'} aria2c\n\n"
                f"🕐 `{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
            )
            try:
                await bot.send_message(OWNER_ID, txt)
            except Exception as e:
                logger.warning(f"Startup msg failed: {e}")

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _sig_handler():
            logger.info("Shutdown signal received")
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _sig_handler)
            except (NotImplementedError, RuntimeError):
                pass

        await stop_event.wait()
        try:
            await bot.stop()
        except Exception as e:
            logger.warning(f"bot.stop() warning (safe to ignore): {e}")

    logger.info("Starting with Pyrogram...")
    start_health_server()   # bind port BEFORE asyncio.run so Render health check passes
    asyncio.run(main())


# ═════════════════════════════════════════════════════════════════════════════
#  TELETHON BOT
# ═════════════════════════════════════════════════════════════════════════════

def run_telethon():
    from telethon import TelegramClient, events

    workers = SETTINGS.get("workers", 4)
    bot     = TelegramClient("gdrive_bot_telethon", API_ID, API_HASH)

    async def tl_send(client, chat_id, status_msg, fp, filename=None, file_size=None, thumb=None, **extra):
        is_path = isinstance(fp, Path)
        fname   = filename or (fp.name if is_path else "file")
        ext     = Path(fname).suffix.lower()
        fsize   = fp.stat().st_size if is_path else (file_size or 0)
        index_prefix = extra.get("_drm_index", "")
        index_part   = f"{index_prefix} " if index_prefix else ""
        _wm_tag_tl = load_settings().get("watermark", "")
        if _wm_tag_tl:
            _stem_tl = Path(fname).stem
            _ext_tl  = Path(fname).suffix
            fname = f"{_stem_tl} {_wm_tag_tl}{_ext_tl}"
        caption = (f"{index_part}✅ **{fname}**\n📦 {human_size(fsize)}"
                   if fsize else f"{index_part}✅ **{fname}**")
        src     = str(fp) if is_path else fp
        _tl_last = [0.0]

        async def progress(sent, total):
            now = time.time()
            if now - _tl_last[0] < 3: return
            _tl_last[0] = now
            try:
                line = upload_bar(sent, total) if total else f"📤 {human_size(sent)}"
                await status_msg.edit(f"📤 **{fname}**\n{line}")
            except Exception: pass

        # For video files: buffer stream to disk if needed (send_file may seek),
        # then attach DocumentAttributeVideo so Telegram never misidentifies as GIF.
        import json as _json
        import tempfile as _tf2

        actual_src  = src
        tmp_vid_tl  = None
        import asyncio as _aio

        # Note: handle_direct uses disk_only=True for Telethon, so fp is always
        # a Path here for direct links. Stream objects (non-Path) should not reach
        # tl_send in normal operation. Guard kept for safety (e.g. gdrive streams).
        if not is_path:
            logger.warning(f"[TL] Unexpected stream object for {fname} — buffering to disk")
            _t = _tf2.NamedTemporaryFile(delete=False, suffix=ext, dir="/tmp")
            def _buf_tl():
                while True:
                    chunk = fp.read(4 * 1024 * 1024)
                    if not chunk: break
                    _t.write(chunk)
                _t.flush()
            await _aio.get_running_loop().run_in_executor(None, _buf_tl)
            _t.close()
            tmp_vid_tl = _t.name
            actual_src = tmp_vid_tl

        if ext in VIDEO_EXTS:
            if not is_path:
                pass  # already buffered above

            # Extract thumbnail if not provided
            if not thumb:
                try:
                    _thumb_dir = str(Path(actual_src).parent)
                    thumb = await _aio.get_running_loop().run_in_executor(
                        None, extract_thumbnail, actual_src, _thumb_dir
                    )
                    if thumb:
                        logger.info(f"[THUMB] Extracted: {thumb}")
                    else:
                        logger.warning(f"[THUMB] Failed to extract thumbnail for {actual_src}")
                except Exception as _te:
                    logger.warning(f"[THUMB] Exception: {_te}")

            # Add silent audio track if missing — prevents GIF conversion
            _fixed_tl  = await _aio.get_running_loop().run_in_executor(None, ensure_audio_track, actual_src)

            if _fixed_tl != actual_src:
                if tmp_vid_tl: 
                    try: Path(tmp_vid_tl).unlink()
                    except Exception: pass
                tmp_vid_tl = _fixed_tl
                actual_src = _fixed_tl

            try:
                from telethon.tl.types import DocumentAttributeVideo
                result = subprocess.run(
                    ["ffprobe", "-v", "quiet", "-print_format", "json",
                     "-show_streams", "-select_streams", "v:0", actual_src],
                    capture_output=True, text=True, timeout=15,
                )
                stream   = _json.loads(result.stdout).get("streams", [{}])[0]
                width    = int(stream.get("width") or 0) or 1280
                height   = int(stream.get("height") or 0) or 720
                duration = int(float(stream.get("duration") or "0"))
                attributes = [DocumentAttributeVideo(
                    duration=duration, w=width, h=height,
                    supports_streaming=True,
                )]
            except Exception:
                attributes = []
        else:
            attributes = None   # let Telethon auto-detect for audio/documents

        try:
            await client.send_file(
                chat_id, actual_src, caption=caption,
                attributes=attributes,
                supports_streaming=ext in VIDEO_EXTS,
                force_document=ext not in (VIDEO_EXTS | AUDIO_EXTS),
                thumb=thumb if ext in VIDEO_EXTS else None,
                part_size_kb=512,
                progress_callback=progress,
            )
        finally:
            if tmp_vid_tl:
                try: Path(tmp_vid_tl).unlink()
                except Exception: pass
            if thumb:
                try: Path(thumb).unlink()
                except Exception: pass
        if is_path:
            try: fp.unlink()
            except Exception: pass
        logger.info(f"Sent {fname} | /tmp: {get_tmp_usage()}")
        try: await status_msg.delete()
        except Exception: pass

    @bot.on(events.NewMessage(pattern="/start"))
    async def start(event):
        await event.reply(
            f"👋 **Universal Downloader Bot**\n"
            f"🔧 Engine: `Telethon` | Workers: `{workers}`\n\n"
            f"✅ Google Drive • Direct links • YouTube/Instagram/TikTok/etc • Magnets\n"
            f"/help — usage | /settings — config"
        )

    @bot.on(events.NewMessage(pattern="/help"))
    async def help_cmd(event):
        await event.reply(
            "📖 **Supported links:**\n\n"
            "• Google Drive (file/folder)\n"
            "• Direct HTTP/HTTPS file links\n"
            "• YouTube, Instagram, Twitter/X, TikTok + 1000 more (yt-dlp)\n"
            "• `.m3u8` / HLS streams\n"
            "• Magnet links (aria2c required)\n"
            "• 🆕 Stream pages — kisi bhi webpage ka link do,\n"
            "  bot automatically video URL dhundh ke download karega\n\n"
            "⚠️ Max 2 GB | One download at a time\n"
            "/cancel — stop current download"
        )

    @bot.on(events.NewMessage(pattern="/settings"))
    async def settings_cmd(event):
        await event.reply(settings_text())

    @bot.on(events.NewMessage(pattern=r"^/set(?:\s|$)"))
    async def set_cmd(event):
        parts = event.raw_text.strip().split()
        await handle_set_cmd(parts, event.reply)

    @bot.on(events.NewMessage(pattern="/cancel"))
    async def cancel_cmd(event):
        global _drm_batch_active, _drm_stop_after_current
        lock = get_download_lock()
        if not lock.locked() and not _drm_batch_active:
            await event.reply("ℹ️ No download is currently running.")
            return
        if _drm_batch_active:
            # DRM batch: let current item finish, then stop
            _drm_stop_after_current = True
            await event.reply("🚫 Current download will finish, then batch will stop.")
        else:
            get_cancel_event().set()
            await event.reply("🚫 Cancel signal sent. Download will stop shortly...")

    @bot.on(events.NewMessage(pattern="/drm"))
    async def drm_cmd(event):
        uid = event.sender_id
        drm_sessions[uid] = {"state": "awaiting_file", "links": []}
        await event.reply(
            "📄 **DRM Link Downloader**\n\n"
            "Ek `.txt` file bhejo jisme links hon (ek line = ek link).\n"
            "Bot links read karke index dikhayega."
        )

    @bot.on(events.NewMessage(func=lambda e: e.message.document is not None))
    async def drm_file_handler(event):
        uid = event.sender_id
        session = drm_sessions.get(uid)
        if not session or session.get("state") != "awaiting_file":
            return

        doc   = event.message.document
        attrs = getattr(doc, "attributes", [])
        fname = ""
        for a in attrs:
            if hasattr(a, "file_name"):
                fname = a.file_name or ""
                break

        if not fname.lower().endswith(".txt"):
            await event.reply("❌ Sirf `.txt` file bhejo.")
            return

        await event.reply("📥 File read kar raha hoon...")
        tmp = tempfile.mkdtemp(dir="/tmp")
        try:
            dl_path = os.path.join(tmp, fname or "links.txt")
            await event.message.download_media(file=dl_path)
            text = Path(dl_path).read_text(encoding="utf-8", errors="ignore")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        links = [l.strip() for l in text.splitlines() if l.strip() and l.strip().startswith("http")]
        if not links:
            await event.reply("❌ File mein koi valid HTTP link nahi mila.")
            drm_sessions.pop(uid, None)
            return

        drm_sessions[uid] = {"state": "awaiting_index", "links": links}

        reply = (
            f"✅ **{len(links)} links mili!**\n\n"
            f"📥 Konsa index download karna hai? (1–{len(links)})\n"
            f"Single `5` → 5 se end tak sab\n"
            f"Multiple: `1 3 5` | Range: `2-5`\n"
            f"Cancel: `/cancel`"
        )
        await event.reply(reply)

    @bot.on(events.NewMessage(func=lambda e: e.is_private or e.is_group))
    async def handle_message(event):
        text = event.raw_text.strip()
        if not text or text.startswith("/"): return
        # Skip messages that arrived before bot started (queued while offline)
        if event.message.date and event.message.date.timestamp() < BOT_START_TIME:
            return
        # Ignore messages from other bots
        sender = await event.get_sender()
        if getattr(sender, "bot", False): return

        uid     = event.sender_id
        chat_id = event.chat_id

        # ── DRM index selection ───────────────────────────────────────────────
        session = drm_sessions.get(uid)
        if session and session.get("state") == "awaiting_index":
            links   = session["links"]
            tokens  = text.split()
            indices = set()

            # Single number → download from that index to end
            if len(tokens) == 1 and tokens[0].isdigit():
                start = int(tokens[0])
                indices.update(range(start, len(links) + 1))
            else:
                for token in tokens:
                    if "-" in token:
                        parts = token.split("-", 1)
                        try:
                            a, b = int(parts[0]), int(parts[1])
                            indices.update(range(a, b + 1))
                        except ValueError:
                            pass
                    else:
                        try: indices.add(int(token))
                        except ValueError: pass

            valid = sorted(i for i in indices if 1 <= i <= len(links))
            if not valid:
                await event.reply(
                    f"❌ Invalid index. 1 se {len(links)} ke beech number bhejo.\n"
                    f"Example: `1` ya `1 3` ya `2-5`"
                )
                return

            drm_sessions.pop(uid, None)

            total_items   = len(valid)
            success_count = 0
            fail_count    = 0

            # Reset flags before starting the batch
            get_cancel_event().clear()
            global _drm_batch_active, _drm_stop_after_current
            _drm_batch_active        = True
            _drm_stop_after_current  = False
            cancelled_mid_batch      = False
            try:
             for idx in valid:
                # Check if /cancel was sent after the previous item finished
                if _drm_stop_after_current:
                    remaining = total_items - success_count - fail_count
                    await event.reply(
                        f"🚫 **Batch stopped after item {idx-1}.**\n"
                        f"✅ Success: {success_count} | ❌ Failed: {fail_count} | ⏭ Skipped: {remaining}"
                    )
                    cancelled_mid_batch = True
                    break

                link    = links[idx - 1]
                tmp_dir = tempfile.mkdtemp(dir="/tmp")

                def _make_send(s): return lambda fp, **kw: tl_send(bot, chat_id, s, fp, **kw)
                def _make_edit(s):
                    async def _edit(t): await s.edit(t)
                    return _edit

                ok = await handle_drm_download(
                    _make_send, _make_edit,
                    lambda t: event.reply(t),
                    uid, link, tmp_dir,
                    idx=idx, total=len(links),
                )
                if ok:
                    success_count += 1
                else:
                    fail_count += 1

             if not cancelled_mid_batch:
              # Completion summary
              await event.reply(
                  f"🏁 **Batch complete!**\n\n"
                  f"✅ Success: `{success_count}`\n"
                  f"❌ Failed:  `{fail_count}`\n"
                  f"📦 Total:   `{total_items}`"
              )
            finally:
             _drm_batch_active       = False
             _drm_stop_after_current = False
            return
        # ── end DRM ──────────────────────────────────────────────────────────
        if link_type == "unknown" or not identifier:
            await event.reply("❓ Unsupported link. Use /help."); return
        lock = get_download_lock()
        if lock.locked():
            wait_msg = await event.reply("⏳ Another download in progress. You are queued — please wait...")
            await lock.acquire()
            try: await wait_msg.delete()
            except Exception: pass
        else:
            await lock.acquire()

        status  = await event.reply("⏳ Processing...")
        tmp_dir = tempfile.mkdtemp(dir="/tmp")
        chat_id = event.chat_id

        async def send_fn(fp, **kw): await tl_send(bot, chat_id, status, fp, **kw)
        async def edit(t):
            try: await status.edit(t)
            except Exception: pass

        global _active_tmp_dir
        _active_tmp_dir = tmp_dir
        cancel = get_cancel_event()
        cancel.clear()
        try:
            if True:  # lock already acquired above
                if   link_type == "gdrive_folder": await handle_gdrive_folder(send_fn, edit, identifier, tmp_dir)
                elif link_type == "gdrive_file":   await handle_gdrive_file(send_fn, edit, identifier, tmp_dir)
                elif link_type == "ytdlp":         await handle_ytdlp(send_fn, edit, identifier, tmp_dir)
                elif link_type == "direct":        await handle_direct(send_fn, edit, identifier, tmp_dir)
                elif link_type == "magnet":        await handle_magnet(send_fn, edit, identifier, tmp_dir)
                elif link_type == "stream_page":   await handle_stream_page(send_fn, edit, identifier, tmp_dir)
        except asyncio.CancelledError:
            try: await status.edit("🚫 Download cancelled.")
            except Exception: pass
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            try: await status.edit(f"❌ **Error:** {e}")
            except Exception: pass
        finally:
            cancel.clear()
            _active_tmp_dir = None
            shutil.rmtree(tmp_dir, ignore_errors=True)
            try: lock.release()
            except RuntimeError: pass
            logger.info(f"/tmp after cleanup: {get_tmp_usage()}")

    async def _run():
        await bot.start(bot_token=BOT_TOKEN)
        logger.info("Starting with Telethon...")

        if OWNER_ID:
            s   = load_settings()
            txt = (
                f"✅ **Bot Started!**\n\n"
                f"🔧 Engine: `Telethon`\n"
                f"👷 Workers: `{s['workers']}`\n"
                f"💾 Max DL: `{s['max_dl_gb']} GB`\n"
                f"✂️ Split: `1.95 GB` per part\n"
                f"{'✅' if check_cmd('yt-dlp') else '❌'} yt-dlp | "
                f"{'✅' if check_cmd('aria2c') else '❌'} aria2c\n\n"
                f"🕐 `{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
            )
            try:
                await bot.send_message(OWNER_ID, txt)
            except Exception as e:
                logger.warning(f"Startup msg failed: {e}")

        await bot.run_until_disconnected()

    start_health_server()   # bind port before asyncio.run
    asyncio.run(_run())


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN: raise ValueError("BOT_TOKEN not set!")
    if not API_ID:    raise ValueError("TELEGRAM_API_ID not set!")
    if not API_HASH:  raise ValueError("TELEGRAM_API_HASH not set!")

    lib = SETTINGS.get("library", "pyrogram")
    logger.info(f"Library: {lib} | Workers: {SETTINGS['workers']} | Max DL: {SETTINGS['max_dl_gb']} GB")

    if check_cmd("yt-dlp"):  logger.info("✅ yt-dlp found")
    else:                     logger.warning("⚠️  yt-dlp missing — pip install yt-dlp")
    if check_cmd("aria2c"):  logger.info("✅ aria2c found")
    else:                     logger.warning("⚠️  aria2c missing — apt install aria2")

    if lib == "telethon":
        run_telethon()
    else:
        run_pyrogram()


if __name__ == "__main__":
    main()
