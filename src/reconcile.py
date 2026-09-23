"""Three-way classification with incoming-wins conflicts and no target deletions.

The caller MUST hold the data lock and remove USB ownership before calling.
Manifest hashes, not FAT timestamps, distinguish network edits from target edits.
"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
from common import SafetyError, atomic_json, copy_atomic, hash_file, read_json, scan, signature


def _parents(path):
    return [p.as_posix() for p in PurePosixPath(path).parents if p.as_posix() != "."]


def _compatible(path, kind, tree):
    nodes = {p.casefold(): (p, "file") for p in tree["files"]}
    nodes.update({p.casefold(): (p, "dir") for p in tree["dirs"]})
    for parent in _parents(path):
        old = nodes.get(parent.casefold())
        if old and old != (parent, "dir"):
            return False
    old = nodes.get(path.casefold())
    return old is None or old == (path, kind)


def _add_dirs(root, path, tree, owner):
    for d in reversed(_parents(path)):
        p = root / d
        if not p.exists():
            p.mkdir()
            if owner is not None:
                os.chown(p, *owner)
        if d not in tree["dirs"]:
            tree["dirs"].append(d)


def _duplicate_name(original, digest, tree, force_root=False):
    p = PurePosixPath(original)
    # A edited duplicate does not accumulate repeated '.target-' suffixes.
    stem = re.sub(r"\.target-[0-9a-f]{12,64}(?:-\d+)?$", "", p.stem)
    while len(stem.encode("utf-8")) > 110:
        stem = stem[:-1]
    suffix = p.suffix if len(p.suffix.encode("utf-8")) <= 30 else ""
    parent = p.parent.as_posix()
    if parent == ".":
        parent = ""
    if force_root:
        parent = ""
        stem = "target-" + hashlib.sha256(original.encode()).hexdigest()[:12] + "-" + stem
    for attempt in range(1000):
        tail = "" if attempt == 0 else "-" + str(attempt)
        name = stem + ".target-" + digest[:16] + tail + suffix
        rel = (PurePosixPath(parent) / name).as_posix()
        if not _compatible(rel, "file", tree):
            # File/directory or case collisions: use a deterministic root fallback.
            key = hashlib.sha256(original.encode()).hexdigest()[:12]
            rel = "target-" + key + "-" + name
        if not _compatible(rel, "file", tree):
            continue
        existing = tree["files"].get(rel)
        if existing is None or existing["hash"] == digest:
            return rel
    raise SafetyError("Unable to allocate a duplicate filename")


def reconcile(incoming, target, manifest_path, owner=None):
    incoming, target, manifest_path = Path(incoming), Path(target), Path(manifest_path)
    base = read_json(manifest_path, {"version": 1, "files": {}, "dirs": []})
    if base.get("version") != 1:
        raise SafetyError("Unsupported sync-state version")
    net, usb = scan(incoming), scan(target)
    initial_net_signature = signature(net)
    report = {"imported": [], "duplicates": [], "published": [], "removed_from_target": []}
    imports = []
    oldfiles, olddirs = base["files"], set(base["dirs"])
    netdirs_at_start = set(net["dirs"])

    def deleted_parent(path):
        return any(d in olddirs and d not in netdirs_at_start for d in _parents(path))

    # Decide from the original USB snapshot; newly copied files cannot feed back.
    for rel, entry in sorted(usb["files"].items()):
        n = net["files"].get(rel)
        old = oldfiles.get(rel)
        oldhash = old["hash"] if old else None
        if n and n["hash"] == entry["hash"]:
            continue
        if entry["hash"] == oldhash:
            # Only the network side changed/deleted: do not resurrect old USB data.
            continue
        if n is None and old is None and not deleted_parent(rel) and _compatible(rel, "file", net):
            dest = rel
            category = "imported"
        else:
            dest = _duplicate_name(rel, entry["hash"], net, force_root=deleted_parent(rel))
            category = "duplicates"
        if dest in net["files"] and net["files"][dest]["hash"] == entry["hash"]:
            continue
        # Reserve destination in the in-memory tree before handling another file.
        net["files"][dest] = dict(entry)
        for d in _parents(dest):
            if d not in net["dirs"]:
                net["dirs"].append(d)
        imports.append((rel, dest, entry["hash"], entry["size"]))
        report[category].append({"source": rel, "destination": dest})

    newdirs = []
    for d in sorted(usb["dirs"], key=lambda x: (x.count("/"), x)):
        if (d not in net["dirs"] and d not in olddirs and not deleted_parent(d)
                and _compatible(d, "dir", net)):
            net["dirs"].append(d)
            newdirs.append(d)

    # Do not start imports if the canonical filesystem cannot hold them.
    required = sum(size for _, _, _, size in imports)
    if required + 8 * 1024 * 1024 > shutil.disk_usage(incoming).free:
        raise SafetyError("Insufficient space for target imports; USB image was not changed")
    if signature(scan(incoming, content=False)) != initial_net_signature:
        raise SafetyError("Incoming files changed while scanning; retry without external writers")
    for d in sorted(newdirs, key=lambda x: x.count("/")):
        (incoming / d).mkdir(parents=True, exist_ok=True)
    for rel, dest, digest, _ in imports:
        _add_dirs(incoming, dest, net, owner)
        copy_atomic(target / rel, incoming / dest, digest, owner)
    # Files/directories made by the root sync worker must remain editable by the web user.
    if owner is not None:
        for basepath, dirs, files in os.walk(incoming, followlinks=False):
            for name in dirs + files:
                p = Path(basepath) / name
                if p.is_symlink():
                    raise SafetyError("Symbolic link appeared during sync")
                os.chown(p, *owner, follow_symlinks=False)

    desired = scan(incoming)
    # Fail before deleting USB data if the desired tree cannot fit on the volume.
    fs = os.statvfs(target)
    cluster = max(fs.f_frsize, 4096)
    desired_bytes = sum(((e["size"] + cluster - 1) // cluster) * cluster for e in desired["files"].values())
    managed_old = sum(((e["size"] + cluster - 1) // cluster) * cluster for e in usb["files"].values())
    if desired_bytes + (len(desired["dirs"]) + 2) * cluster > fs.f_bavail * fs.f_frsize + managed_old:
        raise SafetyError("Merged files do not fit on the USB volume; imports are preserved in incoming")

    # Every target version to be removed has either been imported, duplicated, or
    # identified as an unchanged former mirror. Host metadata is excluded entirely.
    for rel in sorted(usb["files"], key=lambda p: -p.count("/")):
        e = desired["files"].get(rel)
        if e is None or e["hash"] != usb["files"][rel]["hash"]:
            (target / rel).unlink()
            report["removed_from_target"].append(rel)
    for d in sorted(usb["dirs"], key=lambda p: -p.count("/")):
        if d not in desired["dirs"]:
            (target / d).rmdir()
    for d in sorted(desired["dirs"], key=lambda p: p.count("/")):
        (target / d).mkdir(exist_ok=True)
    for rel, e in sorted(desired["files"].items()):
        if not (target / rel).exists() or usb["files"].get(rel, {}).get("hash") != e["hash"]:
            copy_atomic(incoming / rel, target / rel, e["hash"])
            report["published"].append(rel)
    # Read-back verification catches copy failures before committing the baseline.
    verified = scan(target)
    if ({p: e["hash"] for p, e in verified["files"].items()} !=
            {p: e["hash"] for p, e in desired["files"].items()} or set(verified["dirs"]) != set(desired["dirs"])):
        raise SafetyError("USB read-back verification failed; do not reconnect yet")
    if signature(scan(incoming, content=False)) != signature(desired):
        raise SafetyError("An external writer changed incoming during sync; retry after stopping it")
    os.sync()
    # Controller moves this candidate to the committed manifest only AFTER unmount.
    candidate = dict(desired, version=1, incoming_signature=signature(desired))
    atomic_json(str(manifest_path) + ".candidate", candidate)
    return report
