"""Authenticated, rootless Flask file manager for the canonical incoming tree."""
from __future__ import annotations
from datetime import timedelta
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import tempfile
import time
import uuid
from flask import Flask, Request, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash
from common import FAT_MAX, SafetyError, atomic_json, fsync_dir, hash_file, load_config, lock, read_json, relative_path, safe_path, validate_name
from controller import usb_state


def describe_target(usb):
    details = usb.get("udc_details") or {}
    states = [v.get("state", "") for v in details.values()]
    speeds = [v.get("speed", "") for v in details.values() if v.get("speed")]
    functions = [v.get("function", "") for v in details.values() if v.get("function")]
    if not states:
        states = list((usb.get("udc") or {}).values())
    state = next((v for v in states if v), "unknown")
    speed = next((v for v in speeds if v and v != "UNKNOWN"), next(iter(speeds), "UNKNOWN"))
    function = next(iter(functions), "g_mass_storage" if usb.get("module_loaded") else "")
    media = next((v for v in (usb.get("lun_files") or {}).values() if v), "")

    if not usb.get("module_loaded"):
        code, label = "offline", "USB disconnected"
        message = "The target should not see MobileUSB."
    elif usb.get("host_ejected"):
        code, label = "ejected", "Media ejected"
        message = "The USB function is present, but the target has ejected the storage media."
    elif "configured" in states:
        code, label = "configured", "Connected / configured"
        message = "The target should see MobileUSB as a USB mass-storage drive."
    elif any(v not in ("", "not attached", "unknown") for v in states):
        code, label = "enumerating", "USB enumerating"
        message = "The target is negotiating the USB connection; storage may not be visible yet."
    else:
        code, label = "not-attached", "Not attached"
        message = "The gadget is loaded, but no USB host connection is detected."

    return {"code": code, "label": label, "message": message, "state": state,
            "speed": speed or "UNKNOWN", "function": function or "unknown",
            "backing_image": media or "not currently assigned"}


def create_app(config=None):
    cfg = config or load_config()
    root, tmp, trash, state, queue = [Path(cfg[k]) for k in ("incoming", "tmp", "trash", "state", "requests")]
    auth = read_json(cfg["auth"])
    app = Flask(__name__)
    app.config.update(SECRET_KEY=auth["secret"], MAX_CONTENT_LENGTH=FAT_MAX + 8 * 1024 * 1024,
                      MAX_FORM_MEMORY_SIZE=2 * 1024 * 1024, MAX_FORM_PARTS=1000,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=8))

    class DiskRequest(Request):
        def _get_file_stream(self, total_content_length, content_type, filename=None, content_length=None):
            return tempfile.TemporaryFile(mode="wb+", dir=tmp)
    app.request_class = DiskRequest
    failures = {}

    def csrf():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    @app.before_request
    def guard():
        if request.endpoint == "static":
            return
        if request.endpoint == "login" and request.content_length and request.content_length > 16384:
            abort(413)
        if request.endpoint not in ("login", "static") and not session.get("authenticated"):
            return redirect(url_for("login"))
        if request.method == "POST":
            supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf", "")
            if not hmac.compare_digest(session.get("csrf", "missing").encode(), supplied.encode()):
                abort(400, "CSRF check failed. Reload the page and try again.")

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.context_processor
    def context():
        return {"csrf": csrf, "username": auth["username"]}

    @app.template_filter("size")
    def size(n):
        if n is None:
            return "unknown"
        n = float(n)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if n < 1024 or unit == "TiB":
                return f"{n:.1f} {unit}"
            n /= 1024

    @app.template_filter("date")
    def date(value):
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value)) if value else "not yet"

    @app.errorhandler(SafetyError)
    def safety_error(error):
        return render_template("message.html", title="Operation stopped", message=str(error)), 409

    @app.errorhandler(OSError)
    def io_error(error):
        return render_template("message.html", title="Storage operation failed", message="Check free microSD space and permissions. " + str(error)), 507

    @app.errorhandler(413)
    def too_large(error):
        return render_template("message.html", title="Upload too large", message="Each file must fit FAT32 (less than 4 GiB). Split very large batches."), 413

    def target(rel="", allow_root=True):
        return safe_path(root, rel, allow_root)

    def pending():
        atomic_json(queue / "pending", {"time": time.time()}, mode=0o600)

    def ensure_unique_case(p, ignore=None):
        if not p.parent.is_dir():
            raise SafetyError("Parent folder does not exist")
        for child in p.parent.iterdir():
            if child.name.casefold() == p.name.casefold() and child != ignore:
                raise SafetyError("A file/folder with that name already exists (case-insensitive)")

    def trash_item(p):
        if not p.exists():
            raise SafetyError("File no longer exists")
        if p.is_symlink():
            raise SafetyError("Symbolic links are not supported")
        ident = uuid.uuid4().hex
        folder = trash / ident
        folder.mkdir(mode=0o700)
        atomic_json(folder / "meta.json", {"path": p.relative_to(root).as_posix(), "time": time.time()}, mode=0o600)
        os.replace(p, folder / "data")
        fsync_dir(p.parent)
        fsync_dir(folder)
        return ident

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            key = request.remote_addr or "unknown"
            recent = [t for t in failures.get(key, []) if t > time.time() - 60]
            if len(recent) >= 8:
                abort(429, "Too many login attempts. Wait a minute.")
            if (hmac.compare_digest(request.form.get("username", "").encode(), auth["username"].encode())
                    and check_password_hash(auth["password_hash"], request.form.get("password", ""))):
                session.clear()
                session["authenticated"] = True
                session.permanent = True
                csrf()
                failures.pop(key, None)
                return redirect(url_for("index"))
            failures[key] = recent + [time.time()]
            flash("Incorrect username or password.")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        rel = relative_path(request.args.get("path", ""))
        p = target(rel)
        if not p.is_dir():
            abort(404)
        entries = []
        with lock(state / "data.lock", shared=True, timeout=2):
            for child in p.iterdir():
                if child.is_symlink():
                    continue
                s = child.stat()
                entries.append({"name": child.name, "path": child.relative_to(root).as_posix(),
                                "dir": child.is_dir(), "size": s.st_size, "mtime": s.st_mtime})
        entries.sort(key=lambda v: (not v["dir"], v["name"].casefold()))
        parent = str(Path(rel).parent) if rel else ""
        if parent == ".":
            parent = ""
        return render_template("index.html", entries=entries, path=rel, parent=parent, status=get_status())

    def get_status():
        value = read_json(state / "status.json", {})
        value["sd_free"] = shutil.disk_usage(root).free
        value["queued"] = (queue / "pending").exists() or value.get("pending", False)
        value["usb_control_pending"] = (queue / "control.json").exists()
        try:
            live = usb_state(cfg)
            value["usb_live"] = live
            value["target_view"] = describe_target(live)
        except (OSError, SafetyError) as error:
            live = value.get("usb", {})
            value["usb_live"] = live
            value["target_view"] = describe_target(live)
            value["target_view"]["read_error"] = str(error)
        return value

    @app.get("/status")
    def status():
        return jsonify(get_status())

    @app.get("/download")
    def download():
        p = target(request.args.get("path", ""), False)
        if not p.is_file():
            abort(404)
        # Keep the inode open if a later web rename/replace happens.
        with lock(state / "data.lock", shared=True, timeout=2):
            stream = open(p, "rb")
        return send_file(stream, as_attachment=True, download_name=p.name, mimetype="application/octet-stream")

    @app.post("/upload")
    def upload():
        rel = relative_path(request.form.get("path", ""))
        folder = target(rel)
        if not folder.is_dir():
            raise SafetyError("Upload folder does not exist")
        uploads = request.files.getlist("files")
        prepared = []
        try:
            for upload in uploads:
                if not upload.filename:
                    continue
                name = validate_name(upload.filename)
                fd, filename = tempfile.mkstemp(prefix="upload-", dir=tmp)
                prepared.append((name, Path(filename)))
                size = 0
                with os.fdopen(fd, "wb") as f:
                    while True:
                        chunk = upload.stream.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > FAT_MAX:
                            raise SafetyError("File exceeds FAT32's single-file limit")
                        f.write(chunk)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(filename, 0o644)
            with lock(state / "data.lock", timeout=15):
                for name, temp in prepared:
                    dest = target((Path(rel) / name).as_posix())
                    match = next((p for p in folder.iterdir() if p.name.casefold() == name.casefold()), None)
                    if match:
                        if request.form.get("replace") == "yes" and match.name == name and match.is_file():
                            trash_item(match)
                        else:
                            counter = 1
                            while any(p.name.casefold() == dest.name.casefold() for p in folder.iterdir()):
                                dest = folder / f"{Path(name).stem}_{counter}{Path(name).suffix}"
                                counter += 1
                            validate_name(dest.name)
                    os.replace(temp, dest)
                fsync_dir(folder)
                if prepared:
                    pending()
            flash(f"{len(prepared)} file(s) saved. Changes are queued until safe USB synchronization.")
            return redirect(url_for("index", path=rel))
        finally:
            for _, path in prepared:
                path.unlink(missing_ok=True)

    @app.post("/mkdir")
    def mkdir():
        rel = relative_path(request.form.get("path", ""))
        name = validate_name(request.form.get("name", ""))
        with lock(state / "data.lock", timeout=15):
            p = target((Path(rel) / name).as_posix(), False)
            ensure_unique_case(p)
            p.mkdir()
            fsync_dir(p.parent)
            pending()
        return redirect(url_for("index", path=rel))

    @app.route("/action", methods=["GET", "POST"])
    def action():
        rel = relative_path(request.values.get("path", ""), False)
        kind = request.values.get("kind", "")
        if kind not in ("rename", "delete", "edit"):
            abort(400)
        p = target(rel, False)
        if not p.exists():
            abort(404)
        content, digest = "", ""
        if request.method == "POST":
            with lock(state / "data.lock", timeout=15):
                if kind == "delete":
                    trash_item(p)
                elif kind == "rename":
                    dest = p.with_name(validate_name(request.form.get("name", "")))
                    ensure_unique_case(dest, ignore=p)
                    if dest != p:
                        os.rename(p, dest)
                        fsync_dir(dest.parent)
                elif kind == "edit":
                    if not p.is_file() or p.stat().st_size > 1024 * 1024:
                        raise SafetyError("The text editor is limited to UTF-8 files up to 1 MiB")
                    if not hmac.compare_digest(hash_file(p).encode(), request.form.get("digest", "").encode()):
                        raise SafetyError("File changed since the editor opened. Reload it before saving")
                    data = request.form.get("content", "").encode("utf-8")
                    if len(data) > 1024 * 1024:
                        raise SafetyError("Text exceeds the editor limit")
                    fd, name = tempfile.mkstemp(prefix="edit-", dir=tmp)
                    try:
                        with os.fdopen(fd, "wb") as out:
                            out.write(data)
                            out.flush()
                            os.fsync(out.fileno())
                        os.chmod(name, 0o644)
                        trash_item(p)
                        os.replace(name, p)
                        fsync_dir(p.parent)
                    finally:
                        if os.path.exists(name):
                            os.unlink(name)
                pending()
            return redirect(url_for("index", path=p.parent.relative_to(root).as_posix()))
        if kind == "edit":
            with lock(state / "data.lock", shared=True, timeout=2):
                if not p.is_file() or p.stat().st_size > 1024 * 1024:
                    raise SafetyError("The editor supports UTF-8 text files up to 1 MiB")
                try:
                    content = p.read_text(encoding="utf-8")
                except UnicodeError:
                    raise SafetyError("This file is not UTF-8 text")
                digest = hash_file(p)
        return render_template("action.html", path=rel, name=p.name, kind=kind, content=content, digest=digest)

    @app.route("/trash", methods=["GET", "POST"])
    def trash_view():
        if request.method == "POST":
            ident = request.form.get("id", "")
            if len(ident) != 32 or any(c not in "0123456789abcdef" for c in ident):
                abort(400)
            folder = trash / ident
            with lock(state / "data.lock", timeout=15):
                meta = read_json(folder / "meta.json")
                if not meta or not (folder / "data").exists():
                    abort(404)
                if request.form.get("operation") == "restore":
                    dest = target(meta["path"], False)
                    # Do not recreate deleted parents implicitly; the user controls the path.
                    ensure_unique_case(dest)
                    os.replace(folder / "data", dest)
                    shutil.rmtree(folder)
                    fsync_dir(dest.parent)
                    pending()
                elif request.form.get("operation") == "purge" and request.form.get("confirm") == "yes":
                    shutil.rmtree(folder)
                else:
                    abort(400)
            return redirect(url_for("trash_view"))
        items = []
        for p in trash.iterdir():
            if p.is_dir() and not p.is_symlink():
                meta = read_json(p / "meta.json")
                if meta:
                    items.append(dict(meta, id=p.name))
        return render_template("trash.html", items=sorted(items, key=lambda v: -v["time"]))

    @app.post("/usb-control")
    def usb_control():
        rel = relative_path(request.form.get("path", ""))
        action = request.form.get("action", "")
        if action not in ("disconnect", "present"):
            abort(400)
        if action == "disconnect" and request.form.get("target_safe") != "yes":
            raise SafetyError("Force disconnect simulates pulling the USB drive. Confirm the target is idle/ejected before continuing.")
        current = get_status()
        if current.get("phase") == "syncing" or (queue / "refresh.json").exists() or (queue / "control.json").exists():
            raise SafetyError("A USB control or synchronization request is already running or queued")
        payload = {"action": action, "time": time.time(),
                   "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
        if action == "disconnect":
            payload["target_safe"] = True
        atomic_json(queue / "control.json", payload, mode=0o600)
        if action == "disconnect":
            flash("USB disconnect requested. This simulates unplugging the drive; status will update after the controller runs.")
        else:
            flash("USB presentation requested. This reconnects the existing image without synchronizing queued web changes.")
        return redirect(url_for("index", path=rel))

    @app.post("/sync")
    def sync():
        if request.form.get("host_ejected") != "yes":
            raise SafetyError("Eject or unmount the USB drive on the target first, then acknowledge the handoff")
        if get_status().get("phase") == "syncing" or (queue / "refresh.json").exists():
            raise SafetyError("A sync is already running or queued")
        # A fixed request file, not a shell command or path supplied to a root process.
        atomic_json(queue / "refresh.json", {"host_ejected": True, "time": time.time(),
                    "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}, mode=0o600)
        flash("Sync requested. The status will update when the controller runs.")
        return redirect(url_for("index"))
    return app

# Gunicorn imports this factory; tests pass an isolated configuration instead.
if os.environ.get("MOBILEUSB_TESTING") != "1":
    app = create_app()
