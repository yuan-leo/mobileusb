"""Shared filesystem utilities. Trusted local account; no shell interpolation."""
from __future__ import annotations
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import time

FAT_MAX = 2**32 - 1
IGNORED = {"system volume information", "$recycle.bin", ".spotlight-v100", ".trashes", ".fseventsd"}

class SafetyError(RuntimeError):
    pass


def load_config(path=None):
    p = Path(path or os.environ.get("MOBILEUSB_CONFIG", "/etc/mobileusb/config.json"))
    return json.loads(p.read_text())


def read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    with p.open() as f:
        return json.load(f)


def atomic_json(path, value, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".json-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, sort_keys=True, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
            os.fchmod(f.fileno(), mode)
        os.replace(tmp, path)
        fsync_dir(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def lock(path, timeout=30, shared=False):
    # Installer creates the lock in a root-owned, non-writable directory.
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o660)
    start = time.monotonic()
    try:
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - start >= timeout:
                    raise SafetyError("Storage is busy. Try again after synchronization finishes.")
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)


def validate_name(name):
    if (not name or name in (".", "..") or name.endswith((".", " "))
            or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name)):
        raise SafetyError("Filename is not portable to FAT32: " + repr(name))
    # Leave headroom for content-addressed duplicate suffixes.
    if len(name.encode("utf-8")) > 220:
        raise SafetyError("Filename is too long (maximum 220 UTF-8 bytes): " + name)
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", name.split(".")[0]):
        raise SafetyError("Reserved Windows filename: " + name)
    return name


def relative_path(value, allow_root=True):
    if not isinstance(value, str) or "\x00" in value or "\\" in value:
        raise SafetyError("Invalid path")
    if value in ("", ".") and allow_root:
        return ""
    p = PurePosixPath(value)
    if p.is_absolute() or not p.parts or any(v in (".", "..") for v in value.split("/")):
        raise SafetyError("Path must stay inside the file manager")
    for part in p.parts:
        validate_name(part)
    if p.parts[0].casefold() in IGNORED:
        raise SafetyError("This name is reserved for host metadata")
    return p.as_posix()


def safe_path(root, relative, allow_root=True):
    root = Path(root)
    rel = relative_path(relative, allow_root)
    if root.is_symlink() or not root.is_dir():
        raise SafetyError("Storage root is missing or is a symbolic link")
    cur = root
    for part in PurePosixPath(rel).parts:
        cur = cur / part
        if cur.is_symlink():
            raise SafetyError("Symbolic links are not supported")
    return cur


def hash_file(path):
    h = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        before = os.fstat(f.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise SafetyError("Only regular, non-hardlinked files are supported: " + str(path))
        while True:
            data = f.read(1024 * 1024)
            if not data:
                break
            h.update(data)
        after = os.fstat(f.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise SafetyError("File changed during synchronization: " + str(path))
    return h.hexdigest()


def scan(root, content=True):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise SafetyError("Invalid storage root: " + str(root))
    files, dirs, folded = {}, set(), {}
    for base, names, filenames in os.walk(root, followlinks=False):
        if Path(base) == root:
            names[:] = [n for n in names if n.casefold() not in IGNORED]
            filenames = [n for n in filenames if n.casefold() not in IGNORED]
        for name in names + filenames:
            validate_name(name)
            p = Path(base) / name
            s = p.lstat()
            rel = p.relative_to(root).as_posix()
            key = rel.casefold()
            if key in folded:
                raise SafetyError("Case-insensitive filename collision: " + folded[key] + " / " + rel)
            folded[key] = rel
            if stat.S_ISLNK(s.st_mode) or not (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode)):
                raise SafetyError("Unsupported special file: " + rel)
            if stat.S_ISDIR(s.st_mode):
                dirs.add(rel)
            else:
                if s.st_nlink != 1 or s.st_size > FAT_MAX:
                    raise SafetyError("Hardlinked file or file larger than FAT32 permits: " + rel)
                files[rel] = {"size": s.st_size, "mtime_ns": s.st_mtime_ns}
                if content:
                    files[rel]["hash"] = hash_file(p)
    return {"files": files, "dirs": sorted(dirs)}


def signature(tree):
    items = [(k, v["size"], v["mtime_ns"]) for k, v in sorted(tree["files"].items())]
    return hashlib.sha256(json.dumps([items, sorted(tree["dirs"])], ensure_ascii=True).encode()).hexdigest()


def copy_atomic(src, dst, expected=None, owner=None):
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink():
        raise SafetyError("Refusing to replace a symbolic link")
    fd, tmp = tempfile.mkstemp(prefix=".mobileusb-copy-", dir=dst.parent)
    h = hashlib.sha256()
    try:
        infd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(infd, "rb") as inp, os.fdopen(fd, "wb") as out:
            if not stat.S_ISREG(os.fstat(inp.fileno()).st_mode):
                raise SafetyError("Not a regular source file")
            while True:
                chunk = inp.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
            out.flush()
            os.fsync(out.fileno())
            if expected and h.hexdigest() != expected:
                raise SafetyError("Source changed during copy: " + str(src))
            if owner is not None:
                os.fchown(out.fileno(), *owner)
            os.fchmod(out.fileno(), 0o644)
        os.replace(tmp, dst)
        fsync_dir(dst.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
