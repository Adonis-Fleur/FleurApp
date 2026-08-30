import os
import re
import threading
from pathlib import Path
from functools import wraps

import requests as http_requests
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, send_from_directory, jsonify, make_response, current_app)

from .models import db, Profile, Clip, Config
from .scanner import (
    scan_all_background, scan_profile_background,
    get_config, set_config,
    get_scan_logs, clear_scan_logs, is_scanning, _extract_video_url,
    scan_profile_incremental, start_auto_scanner,
)

bp = Blueprint(
    'leantube',
    __name__,
    url_prefix='/LeanTube',
    template_folder='templates',
    static_folder='static',
)

BP_DIR = os.path.dirname(__file__)
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
ADMIN_PASSWORD = "The Leanoning"


@bp.record_once
def setup(state):
    app = state.app
    app.config.setdefault("MAX_CONTENT_LENGTH", 10 * 1024 * 1024)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(BP_DIR, "leantube.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        try:
            from sqlalchemy import inspect
            inspector = inspect(db.engine)
            cols = [c["name"] for c in inspector.get_columns("profile")]
            if "pattern" not in cols:
                db.session.execute(db.text("ALTER TABLE profile ADD COLUMN pattern VARCHAR(256) DEFAULT 'c/Vice_Clip_{n}'"))
                db.session.commit()
        except Exception:
            pass
        try:
            inspector = inspect(db.engine)
            cols = [c["name"] for c in inspector.get_columns("clip")]
            if "custom_thumbnail" not in cols:
                db.session.execute(db.text("ALTER TABLE clip ADD COLUMN custom_thumbnail VARCHAR(512)"))
                db.session.commit()
        except Exception:
            pass
        try:
            inspector = inspect(db.engine)
            cols = [c["name"] for c in inspector.get_columns("clip")]
            if "video_url" not in cols:
                db.session.execute(db.text("ALTER TABLE clip ADD COLUMN video_url VARCHAR(512)"))
                db.session.commit()
        except Exception:
            pass

        Config.query.get("auto_scan_enabled") or set_config("auto_scan_enabled", "0")
        Config.query.get("auto_scan_interval") or set_config("auto_scan_interval", "60")

    start_auto_scanner(app)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.cookies.get("admin") != "1":
            flash("Admin login required", "error")
            return redirect(url_for("leantube.home"))
        return f(*args, **kwargs)
    return decorated


def _clip_url(profile, clip):
    name = clip.original_name or profile.pattern.replace("{n}", str(clip.clip_number))
    return f"http://{profile.ip}:{profile.port}/{name}"


def _thumb_path(clip):
    if clip.custom_thumbnail:
        p = clip.custom_thumbnail
        if p.startswith("/"):
            p = p.lstrip("/")
        return p
    return clip.thumbnail


@bp.route("/")
def index():
    return redirect(url_for("leantube.home"))


@bp.route("/Home", methods=["GET", "POST"])
def home():
    admin = request.cookies.get("admin") == "1"
    profiles = Profile.query.all()
    clips = Clip.query.order_by(Clip.clip_number.desc()).all()

    if request.method == "POST":
        action = request.form.get("action", "add_profile")

        if action == "admin_login":
            pwd = request.form.get("password", "")
            if pwd == ADMIN_PASSWORD:
                resp = make_response(redirect(url_for("leantube.home")))
                resp.set_cookie("admin", "1", max_age=365*24*3600)
                flash("Logged in as admin (saved as cookie)", "success")
                return resp
            else:
                flash("Wrong password", "error")
                return redirect(url_for("leantube.home"))

        elif action == "admin_logout":
            resp = make_response(redirect(url_for("leantube.home")))
            resp.delete_cookie("admin")
            flash("Logged out", "success")
            return resp

        elif action == "add_profile":
            name = request.form.get("name", "").strip()
            ip = request.form.get("ip", "").strip()
            port = request.form.get("port", "").strip()
            pattern = request.form.get("pattern", "").strip() or "c/Vice_Clip_{n}"
            if name and ip and port.isdigit():
                profile = Profile(name=name, ip=ip, port=int(port), pattern=pattern)
                db.session.add(profile)
                db.session.commit()
                flash(f"Profile '{name}' added, scanning...", "success")
                _app = current_app._get_current_object()
                _thr = threading.Thread(target=scan_profile_incremental, args=(profile.id, _app), daemon=True)
                _thr.start()
            else:
                flash("Invalid profile data", "error")

            return redirect(url_for("leantube.home"))

    no_setup = bool(clips)
    return render_template("index.html", clips=clips, profiles=profiles,
                           admin=admin, no_setup=no_setup)


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    admin = request.cookies.get("admin") == "1"

    if request.method == "POST":
        action = request.form.get("action")

        if action == "admin_login":
            pwd = request.form.get("password", "")
            if pwd == ADMIN_PASSWORD:
                resp = make_response(redirect(url_for("leantube.settings")))
                resp.set_cookie("admin", "1", max_age=365*24*3600)
                flash("Logged in as admin (saved as cookie)", "success")
                return resp
            else:
                flash("Wrong password", "error")
                return redirect(url_for("leantube.settings"))

        elif action == "admin_logout":
            resp = make_response(redirect(url_for("leantube.settings")))
            resp.delete_cookie("admin")
            flash("Logged out", "success")
            return resp

        elif action == "add_profile":
            name = request.form.get("name", "").strip()
            ip = request.form.get("ip", "").strip()
            port = request.form.get("port", "").strip()
            pattern = request.form.get("pattern", "").strip() or "c/Vice_Clip_{n}"
            if name and ip and port.isdigit():
                profile = Profile(name=name, ip=ip, port=int(port), pattern=pattern)
                db.session.add(profile)
                db.session.commit()
                flash("Profile added", "success")
            else:
                flash("Invalid profile data", "error")

        elif action == "update_pattern":
            pid = request.form.get("profile_id")
            pattern = request.form.get("pattern", "").strip()
            if pid and pid.isdigit() and pattern:
                profile = db.session.get(Profile, int(pid))
                if profile:
                    profile.pattern = pattern
                    db.session.commit()
                    flash("Pattern updated", "success")

        elif action == "delete_profile":
            pid = request.form.get("profile_id")
            if pid and pid.isdigit():
                profile = db.session.get(Profile, int(pid))
                if profile:
                    db.session.delete(profile)
                    db.session.commit()
                    flash("Profile deleted", "success")

        elif action == "save_config":
            download_path = request.form.get("download_path", "").strip()
            auto_download = request.form.get("auto_download", "0")
            auto_scan = request.form.get("auto_scan_enabled", "0")
            auto_interval = request.form.get("auto_scan_interval", "60").strip()

            if download_path:
                p = Path(download_path)
                if not p.exists():
                    flash(f"Path does not exist: {download_path}", "error")
                elif not os.access(str(p), os.W_OK):
                    flash(f"Path is not writable: {download_path}", "error")
                else:
                    set_config("download_path", download_path)
            else:
                set_config("download_path", "")

            set_config("auto_download", auto_download)
            set_config("auto_scan_enabled", auto_scan)

            if auto_interval.isdigit() and int(auto_interval) >= 1:
                set_config("auto_scan_interval", auto_interval)
            else:
                flash("Invalid interval, using 60", "error")

            flash("Config saved", "success")

        elif action == "scan":
            if is_scanning():
                flash("A scan is already running", "error")
            else:
                _app = current_app._get_current_object()
                pid = request.form.get("profile_id")
                if pid and pid.isdigit():
                    scan_profile_background(int(pid), _app, full=False)
                    flash("Scan started — check logs below", "success")
                else:
                    from .scanner import scan_all_incremental
                    clear_scan_logs()
                    t = threading.Thread(target=scan_all_incremental, args=(_app,), daemon=True)
                    t.start()
                    flash("Scanning all profiles — check logs below", "success")

        return redirect(url_for("leantube.settings"))

    profiles = Profile.query.all()
    download_path = get_config("download_path", "")
    auto_download = get_config("auto_download", "1")
    auto_scan_enabled = get_config("auto_scan_enabled", "0")
    auto_scan_interval = get_config("auto_scan_interval", "60")
    return render_template("settings.html", profiles=profiles,
                           download_path=download_path, auto_download=auto_download,
                           auto_scan_enabled=auto_scan_enabled,
                           auto_scan_interval=auto_scan_interval,
                           admin=admin)


@bp.route("/api/scan-status")
def api_scan_status():
    logs = get_scan_logs()
    return jsonify({
        "scanning": is_scanning(),
        "logs": logs,
    })


@bp.route("/api/test-url")
def api_test_url():
    url = request.args.get("url", "")
    if not url:
        return jsonify({"ok": False, "error": "no url"})

    try:
        resp = http_requests.get(url, timeout=5)
        status = resp.status_code
        ct = resp.headers.get("Content-Type", "")
        html_preview = ""
        video_url = None
        if status == 200:
            text = resp.text
            html_preview = text[:2000]
            video_url = _extract_video_url(text, url)
        resp.close()
        return jsonify({
            "ok": status == 200,
            "status": status,
            "content_type": ct,
            "video_url": video_url,
            "html_preview": html_preview,
            "url": url,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "url": url})


@bp.route("/api/debug-html")
def api_debug_html():
    url = request.args.get("url", "")
    if not url:
        return jsonify({"ok": False, "error": "no url"})
    try:
        resp = http_requests.get(url, timeout=5)
        text = resp.text
        ct = resp.headers.get("Content-Type", "")
        video_url = _extract_video_url(text, url) if resp.status_code == 200 else None
        return jsonify({
            "ok": resp.status_code == 200,
            "status": resp.status_code,
            "content_type": ct,
            "video_url": video_url,
            "html": text[:5000],
            "html_length": len(text),
            "url": url,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "url": url})


@bp.route("/edit/<int:clip_id>", methods=["GET", "POST"])
@login_required
def edit(clip_id):
    clip = db.session.get(Clip, clip_id)
    if not clip:
        flash("Clip not found", "error")
        return redirect(url_for("leantube.home"))

    if request.method == "POST":
        action = request.form.get("action", "save_name")

        if action == "save_name":
            custom_name = request.form.get("custom_name", "").strip()
            clip.custom_name = custom_name if custom_name else None
            db.session.commit()
            flash("Name updated", "success")

        elif action == "set_thumb_url":
            url = request.form.get("thumb_url", "").strip()
            if url:
                try:
                    r = http_requests.get(url, timeout=10, stream=True)
                    ct = r.headers.get("Content-Type", "")
                    if not ct.startswith("image/"):
                        flash("URL does not point to an image", "error")
                    else:
                        os.makedirs(os.path.join(BP_DIR, "static", "thumbnails"), exist_ok=True)
                        ext = ".jpg"
                        for e in ALLOWED_EXT:
                            if e in ct:
                                ext = e
                                break
                        fname = f"{clip.id}_custom{ext}"
                        save_path = os.path.join(BP_DIR, "static", "thumbnails", fname)
                        with open(save_path, "wb") as f:
                            for chunk in r.iter_content(8192):
                                f.write(chunk)
                        clip.custom_thumbnail = f"thumbnails/{fname}"
                        db.session.commit()
                        flash("Thumbnail set from URL", "success")
                    r.close()
                except Exception as e:
                    flash(f"Failed to fetch image: {e}", "error")
            else:
                flash("Enter an image URL", "error")

        return redirect(url_for("leantube.edit", clip_id=clip.id))

    thumb_src = _thumb_path(clip)
    return render_template("edit.html", clip=clip, thumb_src=thumb_src)


@bp.route("/api/upload-thumbnail/<int:clip_id>", methods=["POST"])
@login_required
def upload_thumbnail(clip_id):
    clip = db.session.get(Clip, clip_id)
    if not clip:
        return jsonify({"ok": False, "error": "not found"})

    if "file" not in request.files:
        return jsonify({"ok": False, "error": "no file"})

    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "empty file"})

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return jsonify({"ok": False, "error": f"invalid type {ext}, allowed: {', '.join(ALLOWED_EXT)}"})

    os.makedirs(os.path.join(BP_DIR, "static", "thumbnails"), exist_ok=True)
    fname = f"{clip.id}_custom{ext}"
    save_path = os.path.join(BP_DIR, "static", "thumbnails", fname)
    f.save(save_path)
    clip.custom_thumbnail = f"thumbnails/{fname}"
    db.session.commit()
    return jsonify({"ok": True, "path": clip.custom_thumbnail})


@bp.route("/api/set-thumbnail-url/<int:clip_id>", methods=["POST"])
@login_required
def set_thumbnail_url(clip_id):
    clip = db.session.get(Clip, clip_id)
    if not clip:
        return jsonify({"ok": False, "error": "not found"})

    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"ok": False, "error": "no url"})

    try:
        r = http_requests.get(url, timeout=10, stream=True)
        ct = r.headers.get("Content-Type", "")
        if not ct.startswith("image/"):
            r.close()
            return jsonify({"ok": False, "error": "not an image"})
        os.makedirs(os.path.join(BP_DIR, "static", "thumbnails"), exist_ok=True)
        ext = ".jpg"
        for e in ALLOWED_EXT:
            if e in ct:
                ext = e
                break
        fname = f"{clip.id}_custom{ext}"
        save_path = os.path.join(BP_DIR, "static", "thumbnails", fname)
        with open(save_path, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        r.close()
        clip.custom_thumbnail = f"thumbnails/{fname}"
        db.session.commit()
        return jsonify({"ok": True, "path": clip.custom_thumbnail})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@bp.route("/api/reset-thumbnail/<int:clip_id>", methods=["POST"])
@login_required
def reset_thumbnail(clip_id):
    clip = db.session.get(Clip, clip_id)
    if not clip:
        return jsonify({"ok": False, "error": "not found"})

    if clip.custom_thumbnail:
        old_path = os.path.join(BP_DIR, "static", clip.custom_thumbnail)
        if os.path.exists(old_path):
            os.remove(old_path)
        clip.custom_thumbnail = None
        db.session.commit()
    return jsonify({"ok": True})


@bp.route("/api/regenerate-thumbnail/<int:clip_id>", methods=["POST"])
@login_required
def regenerate_thumbnail(clip_id):
    from .scanner import _make_thumbnail_remote, _make_thumbnail
    clip = db.session.get(Clip, clip_id)
    if not clip:
        return jsonify({"ok": False, "error": "not found"})

    os.makedirs(os.path.join(BP_DIR, "static", "thumbnails"), exist_ok=True)
    thumb_path = f"thumbnails/{clip.id}.jpg"
    abs_thumb = os.path.join(BP_DIR, "static", thumb_path)

    if clip.local_path and os.path.exists(clip.local_path):
        _make_thumbnail(clip.local_path, abs_thumb)
    else:
        profile = clip.profile
        url = _clip_url(profile, clip)
        try:
            resp = http_requests.get(url, timeout=5)
            if resp.status_code == 200:
                video_url = _extract_video_url(resp.text, url)
                resp.close()
                if video_url:
                    _make_thumbnail_remote(video_url, abs_thumb)
                else:
                    _make_thumbnail_remote(url, abs_thumb)
            else:
                resp.close()
                _make_thumbnail_remote(url, abs_thumb)
        except Exception:
            _make_thumbnail_remote(url, abs_thumb)

    clip.thumbnail = thumb_path
    db.session.commit()
    return jsonify({"ok": True, "thumbnail": thumb_path})


@bp.route("/watch/<int:clip_id>")
def watch(clip_id):
    clip = db.session.get(Clip, clip_id)
    if not clip:
        flash("Clip not found", "error")
        return redirect(url_for("leantube.home"))

    recommended = Clip.query.filter(
        Clip.id != clip.id
    ).order_by(Clip.clip_number.desc()).limit(24).all()

    admin = request.cookies.get("admin") == "1"
    return render_template("watch.html", clip=clip, recommended=recommended, admin=admin)


@bp.route("/video/<int:clip_id>")
def serve_video(clip_id):
    clip = db.session.get(Clip, clip_id)
    if not clip:
        return ("not found", 404)

    if clip.local_path and os.path.exists(clip.local_path):
        return send_from_directory(
            os.path.dirname(clip.local_path),
            os.path.basename(clip.local_path),
            conditional=True,
        )

    target = clip.video_url
    if target:
        return redirect(target)

    from .scanner import _extract_video_url
    page_url = _clip_url(clip.profile, clip)
    try:
        resp = http_requests.get(page_url, timeout=10)
        if resp.status_code == 200:
            target = _extract_video_url(resp.text, page_url)
            resp.close()
            if target:
                clip.video_url = target
                db.session.commit()
                return redirect(target)
    except Exception:
        pass

    return redirect(page_url)
