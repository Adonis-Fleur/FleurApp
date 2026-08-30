import os
import re
import subprocess
import threading
import time as time_module
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from flask import current_app

from .models import db, Profile, Clip, Config

_LEANTUBE_STATIC = os.path.join(os.path.dirname(__file__), 'static')

SCAN_RANGE = 1000
INCREMENTAL_WINDOW = 50
TIMEOUT = 5
MAX_WORKERS = 20
EARLY_STOP_MISSES = 100
THUMBNAIL_SIZE = "320x180"
THUMBNAIL_SEEK = "00:00:01"

_scan_logs = []
_scan_lock = threading.Lock()
_scanning = False


def _log(msg):
    with _scan_lock:
        _scan_logs.append((datetime.now(timezone.utc).isoformat(), msg))

def is_scanning():
    return _scanning

def get_scan_logs():
    with _scan_lock:
        return list(_scan_logs)

def clear_scan_logs():
    with _scan_lock:
        _scan_logs.clear()


def get_config(key, default=None):
    c = Config.query.get(key)
    return c.value if c else default

def set_config(key, value):
    c = Config.query.get(key)
    if c:
        c.value = value
    else:
        c = Config(key=key, value=value)
        db.session.add(c)
    db.session.commit()


def _clip_url(profile, n):
    name = profile.pattern.replace("{n}", str(n))
    return f"http://{profile.ip}:{profile.port}/{name}"


def _extract_video_url(html, page_url):
    patterns = [
        r'<video[^>]*src=["\']([^"\']+)["\']',
        r'<source[^>]*src=["\']([^"\']+)["\']',
        r'"videoUrl"\s*:\s*"([^"]+)"',
        r'"url"\s*:\s*"([^"]+)"',
        r'href=["\']([^"\']+\.(mp4|webm|ogg|mov|avi))["\']',
        r'src=["\']([^"\']+\.(mp4|webm|ogg|mov|avi))["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            url = m.group(1)
            if url.startswith("//"):
                from urllib.parse import urlparse
                parsed = urlparse(page_url)
                url = f"{parsed.scheme}:{url}"
            elif url.startswith("/"):
                from urllib.parse import urlparse
                parsed = urlparse(page_url)
                url = f"{parsed.scheme}://{parsed.netloc}{url}"
            elif not url.startswith("http"):
                from urllib.parse import urljoin
                url = urljoin(page_url, url)
            return url
    return None


def _check_clip(args):
    n, profile = args
    url = _clip_url(profile, n)
    try:
        resp = requests.get(url, timeout=TIMEOUT)
        status = resp.status_code
        ct = resp.headers.get("Content-Type", "")
        if status == 200:
            text = resp.text
            video_url = _extract_video_url(text, url)
            if video_url:
                return (n, url, video_url, None, status, ct)
            return (n, url, None, "NO_VIDEO_SRC", status, ct)
        return (n, url, None, f"HTTP {status}", status, ct)
    except requests.ConnectionError:
        return (n, url, None, "CONN_REFUSED", "CONN_REFUSED", None)
    except requests.Timeout:
        return (n, url, None, "TIMEOUT", "TIMEOUT", None)
    except requests.RequestException as e:
        return (n, url, None, f"ERROR:{e}", "ERROR", None)


def _process_clip_result(result, profile, existing, app):
    n, clip_url, video_url, error, status, ct = result
    now = datetime.now(timezone.utc)

    if error is not None:
        return False

    clip = existing.get(n)
    if clip:
        clip.last_seen = now
        db.session.commit()
        return True

    download_path = get_config("download_path")
    auto_download = get_config("auto_download", "1") == "1"
    original_name = profile.pattern.replace("{n}", str(n))

    clip = Clip(
        profile_id=profile.id,
        clip_number=n,
        original_name=original_name,
        video_url=video_url,
        last_seen=now,
    )
    db.session.add(clip)
    db.session.flush()

    os.makedirs(os.path.join(_LEANTUBE_STATIC, "thumbnails"), exist_ok=True)
    thumb_path = f"thumbnails/{clip.id}.jpg"
    abs_thumb = os.path.join(_LEANTUBE_STATIC, thumb_path)
    target_url = video_url if video_url else clip_url

    if auto_download and download_path:
        profile_dir = os.path.join(download_path, _safe_name(profile.name))
        os.makedirs(profile_dir, exist_ok=True)
        ext = ".mp4"
        if video_url:
            ext_match = re.search(r"\.(\w+)(?:\?|$)", video_url)
            if ext_match:
                ext = f".{ext_match.group(1)}"
        local_file = os.path.join(profile_dir, f"{original_name}{ext}")
        try:
            _log(f"[DOWNLOAD] {original_name} -> {local_file}")
            _download_file(target_url, local_file)
            clip.local_path = local_file
            _make_thumbnail(local_file, abs_thumb)
            clip.thumbnail = thumb_path
        except Exception as e:
            _log(f"[WARN] failed to download/thumbnail {original_name}: {e}")
            if os.path.exists(local_file):
                os.remove(local_file)
            _make_thumbnail_remote(target_url, abs_thumb)
            clip.thumbnail = thumb_path
    else:
        _log(f"[DISCOVERED] {original_name}")
        _make_thumbnail_remote(target_url, abs_thumb)
        clip.thumbnail = thumb_path

    db.session.commit()
    return True


def scan_profile(profile_id, app):
    global _scanning
    with app.app_context():
        profile = db.session.get(Profile, profile_id)
        if not profile:
            _log(f"[ERROR] Profile {profile_id} not found")
            return

        _scanning = True
        pattern_display = profile.pattern.replace("{n}", "N")
        _log(f"[START] Full scan {profile.name} ({profile.ip}:{profile.port}) pattern: {pattern_display}")

        try:
            existing = {c.clip_number: c for c in profile.clips.all()}
            args_list = [(n, profile) for n in range(SCAN_RANGE)]
            found = 0
            misses = 0
            completed = 0
            logged_errors = set()
            no_video_logged = False

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = {pool.submit(_check_clip, a): a[0] for a in args_list}
                for f in as_completed(futures):
                    completed += 1
                    if completed % 50 == 0:
                        _log(f"[PROGRESS] {profile.name}: checked {completed}/{SCAN_RANGE}, found {found}")

                    n, clip_url, video_url, error, status, ct = f.result()
                    clip_name = profile.pattern.replace("{n}", str(n))

                    if error is not None:
                        misses += 1
                        if error not in ("CONN_REFUSED", "TIMEOUT") and not error.startswith("ERROR"):
                            err_key = str(status) if isinstance(status, int) else "NO_VIDEO"
                            if err_key not in logged_errors:
                                logged_errors.add(err_key)
                                _log(f"[HTTP] {profile.name}: {clip_name} -> status {status} (content-type: {ct})")
                        if error == "NO_VIDEO_SRC" and not no_video_logged:
                            no_video_logged = True
                            _log(f"[WARN] {profile.name}: page returned 200 but no video URL found in HTML (first at clip {n})")
                        if misses >= EARLY_STOP_MISSES:
                            _log(f"[EARLY STOP] {profile.name}: {EARLY_STOP_MISSES} consecutive misses at clip {n}")
                            for remaining in futures:
                                remaining.cancel()
                            break
                        continue

                    misses = 0
                    found += 1
                    result = (n, clip_url, video_url, error, status, ct)
                    if _process_clip_result(result, profile, existing, app):
                        _log(f"[NEW] {clip_name}")

            _log(f"[DONE] {profile.name}: full scan finished, found {found} clips total")
        finally:
            _scanning = False


def scan_profile_incremental(profile_id, app, window=INCREMENTAL_WINDOW):
    global _scanning
    with app.app_context():
        profile = db.session.get(Profile, profile_id)
        if not profile:
            _log(f"[ERROR] Profile {profile_id} not found")
            return

        if _scanning:
            _log(f"[SKIP] {profile.name}: another scan is running")
            return

        _scanning = True

        from sqlalchemy import func
        max_n = db.session.query(func.max(Clip.clip_number)).filter(
            Clip.profile_id == profile.id
        ).scalar()

        if max_n is None:
            start = 0
        else:
            start = max_n + 1

        end = start + window
        if end > SCAN_RANGE:
            end = SCAN_RANGE

        if start >= SCAN_RANGE:
            _log(f"[SKIP] {profile.name}: already at clip limit ({SCAN_RANGE})")
            _scanning = False
            return

        _log(f"[INCREMENTAL] {profile.name}: scanning {start} to {end-1} ({window} clips)")

        try:
            existing = {c.clip_number: c for c in profile.clips.all()}
            args_list = [(n, profile) for n in range(start, end)]
            found = 0

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = {pool.submit(_check_clip, a): a[0] for a in args_list}
                for f in as_completed(futures):
                    n, clip_url, video_url, error, status, ct = f.result()

                    if error is not None:
                        continue

                    found += 1
                    result = (n, clip_url, video_url, error, status, ct)
                    clip_name = profile.pattern.replace("{n}", str(n))
                    if _process_clip_result(result, profile, existing, app):
                        _log(f"[NEW] {clip_name}")

            _log(f"[DONE] {profile.name}: incremental scan found {found} new clips")
        finally:
            _scanning = False


def scan_all_incremental(app):
    profiles = Profile.query.all()
    if not profiles:
        _log("[INFO] No profiles to scan")
    for profile in profiles:
        scan_profile_incremental(profile.id, app)


def scan_all(app):
    profiles = Profile.query.all()
    if not profiles:
        _log("[INFO] No profiles to scan")
    for profile in profiles:
        scan_profile(profile.id, app)


def scan_all_background(app):
    clear_scan_logs()
    t = threading.Thread(target=scan_all, args=(app,), daemon=True)
    t.start()


def scan_profile_background(profile_id, app, full=False):
    clear_scan_logs()
    if full:
        t = threading.Thread(target=scan_profile, args=(profile_id, app), daemon=True)
    else:
        t = threading.Thread(target=scan_profile_incremental, args=(profile_id, app), daemon=True)
    t.start()


def _auto_scan_loop(app):
    while True:
        try:
            with app.app_context():
                enabled = get_config("auto_scan_enabled", "0") == "1"
                interval_m = int(get_config("auto_scan_interval", "60"))
            if enabled:
                with app.app_context():
                    _log(f"[AUTO] Auto-scan enabled, running incremental scan (interval: {interval_m}m)")
                    scan_all_incremental(app)
            time_module.sleep(60)
        except Exception:
            time_module.sleep(60)


def start_auto_scanner(app):
    t = threading.Thread(target=_auto_scan_loop, args=(app,), daemon=True)
    t.start()
    _log("[AUTO] Auto-scanner started (checks every 60s)")


def _safe_name(name):
    return "".join(c if c.isalnum() or c in " _-" else "_" for c in name).strip()

def _download_file(url, path):
    r = requests.get(url, stream=True, timeout=TIMEOUT)
    r.raise_for_status()
    with open(path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)

def _make_thumbnail(video_path, thumb_path):
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-ss", THUMBNAIL_SEEK,
             "-vframes", "1", "-s", THUMBNAIL_SIZE, thumb_path],
            capture_output=True, timeout=15,
        )
    except Exception:
        pass

def _make_thumbnail_remote(url, thumb_path):
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", url, "-ss", THUMBNAIL_SEEK,
             "-vframes", "1", "-s", THUMBNAIL_SIZE, thumb_path],
            capture_output=True, timeout=30,
        )
    except Exception:
        pass
