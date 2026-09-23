#!/usr/bin/python3
"""Privileged, fixed-path USB controller. Web server never runs as root."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from common import SafetyError, atomic_json, fsync_dir, load_config, lock, read_json, scan, signature
from reconcile import reconcile


def run(*args, check=True, timeout=120):
    proc = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    if check and proc.returncode:
        raise SafetyError("Command failed: " + " ".join(args) + "\n" + proc.stdout[-6000:])
    return proc


def module_loaded():
    return Path("/sys/module/g_mass_storage").is_dir()


def lun_paths():
    found = []
    for udc in Path("/sys/class/udc").glob("*"):
        root = udc.resolve().parent.parent
        for base, dirs, files in os.walk(root, followlinks=False):
            depth = len(Path(base).relative_to(root).parts)
            if depth > 5:
                dirs[:] = []
                continue
            if Path(base).name.startswith("lun") and "file" in files and "ro" in files:
                found.append(Path(base) / "file")
    return sorted(set(found))


def usb_state(cfg):
    states = {}
    for p in Path("/sys/class/udc").glob("*/state"):
        states[p.parent.name] = p.read_text().strip()
    luns = lun_paths() if module_loaded() else []
    media = {str(p): p.read_text().strip() for p in luns}
    return {"module_loaded": module_loaded(), "udc": states, "lun_files": media,
            "host_ejected": bool(media) and all(not v for v in media.values())}


def status_write(cfg, **updates):
    path = Path(cfg["state"]) / "status.json"
    value = read_json(path, {})
    candidate = dict(value, **updates)
    if candidate != value:
        candidate["updated"] = time.time()
        atomic_json(path, candidate)
    return candidate


def assert_our_image(cfg):
    image = Path(cfg["image"])
    if not image.is_file() or image.is_symlink():
        raise SafetyError("Backing image is missing or is a symbolic link")
    if module_loaded():
        p = Path("/sys/module/g_mass_storage/parameters/file")
        if not p.exists() or p.read_text().strip() != cfg["image"]:
            raise SafetyError("Another mass-storage image is active; refusing to replace it")
    for p in Path("/sys/kernel/config/usb_gadget").glob("*/UDC"):
        if p.read_text().strip():
            raise SafetyError("A ConfigFS gadget is active; it must be retired before this upgrade")


def ensure_not_local(cfg):
    if run("findmnt", "-rn", "--mountpoint", cfg["mount"], check=False).returncode == 0:
        raise SafetyError("The USB image is already mounted locally; leaving it disconnected")
    if run("losetup", "-j", cfg["image"]).stdout.strip():
        raise SafetyError("The image has an existing loop mapping; inspect it before continuing")


def present(cfg):
    assert_our_image(cfg)
    ensure_not_local(cfg)
    if (Path(cfg["state"]) / "recovery-required.json").exists():
        raise SafetyError("A previous sync failed. Use an explicit checked refresh before reconnecting")
    if not module_loaded():
        if not any(Path("/sys/class/udc").glob("*")):
            raise SafetyError("No USB device controller. Check the dwc2 peripheral configuration")
        run("modprobe", "g_mass_storage", "file=" + cfg["image"], "removable=1", "ro=0", "stall=0", "nofua=0")
    status_write(cfg, phase="ready", usb=usb_state(cfg), error=None)


def disconnect(cfg):
    assert_our_image(cfg)
    if module_loaded():
        run("modprobe", "-r", "g_mass_storage")
    if module_loaded():
        raise SafetyError("g_mass_storage did not unload; refusing to access the image")
    os.sync()


def sync_once(cfg, acknowledged=False, boot=False):
    assert_our_image(cfg)
    usb = usb_state(cfg)
    if usb["module_loaded"] and not usb["host_ejected"] and not acknowledged:
        raise SafetyError("USB host still owns the writable disk. Eject/unmount it first; no sync was performed")
    marker = Path(cfg["state"]) / "recovery-required.json"
    with lock(Path(cfg["state"]) / "data.lock", timeout=600):
        status_write(cfg, phase="syncing", error=None)
        disconnect(cfg)
        ensure_not_local(cfg)
        fstype = run("blkid", "-p", "-s", "TYPE", "-o", "value", cfg["image"], check=False).stdout.strip()
        if fstype != "vfat":
            raise SafetyError("This upgrade requires the existing unpartitioned FAT32 image. It will not format or convert it")
        # Read-only check: do not silently repair a damaged target filesystem.
        chk = run("fsck.fat", "-n", cfg["image"], check=False, timeout=600)
        if chk.returncode:
            atomic_json(marker, {"time": time.time(), "reason": chk.stdout[-6000:]})
            raise SafetyError("FAT check reported errors. Image left offline, without automatic repair:\n" + chk.stdout[-5000:])
        atomic_json(marker, {"time": time.time(), "reason": "Sync in progress; retry explicitly if interrupted"})
        status_write(cfg, phase="syncing", error=None, usb=usb_state(cfg))
        mounted = False
        manifest = Path(cfg["state"]) / "manifest.json"
        try:
            Path(cfg["mount"]).mkdir(parents=True, exist_ok=True)
            run("mount", "-t", "vfat", "-o", "loop,rw,nosuid,nodev,noexec,utf8,umask=022", cfg["image"], cfg["mount"])
            mounted = True
            report = reconcile(cfg["incoming"], cfg["mount"], manifest, owner=(cfg["uid"], cfg["gid"]))
            fs = os.statvfs(cfg["mount"])
            free = fs.f_bavail * fs.f_frsize
            total = fs.f_blocks * fs.f_frsize
            os.sync()
            run("umount", cfg["mount"], timeout=600)
            mounted = False
            ensure_not_local(cfg)
            os.replace(str(manifest) + ".candidate", manifest)
            fsync_dir(manifest.parent)
            marker.unlink()
            fsync_dir(marker.parent)
            (Path(cfg["requests"]) / "pending").unlink(missing_ok=True)
            status_write(cfg, last_sync=time.time(), report=report, usb_free=free, usb_capacity=total,
                         pending=False, error=None)
        finally:
            if mounted:
                # Never rebind on failure, including failure to unmount.
                run("umount", cfg["mount"], check=False, timeout=600)
        present(cfg)


def poll(cfg):
    request = Path(cfg["requests"]) / "refresh.json"
    explicit = False
    if request.exists():
        fd = os.open(request, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as f:
            value = json.loads(f.read(4097))
        explicit = (value.get("host_ejected") is True
                    and value.get("boot_id") == Path("/proc/sys/kernel/random/boot_id").read_text().strip()
                    and 0 <= time.time() - float(value.get("time", 0)) <= 120)
        request.unlink()
        if not explicit:
            status_write(cfg, error="Stale or invalid handoff request ignored. Eject the target and confirm again.")
    marker = Path(cfg["state"]) / "recovery-required.json"
    if marker.exists() and not explicit:
        return
    usb = usb_state(cfg)
    if explicit or usb["host_ejected"]:
        sync_once(cfg, acknowledged=explicit)
        return
    # A timer notices subdirectory edits too, but never auto-detaches a writable host.
    with lock(Path(cfg["state"]) / "data.lock", timeout=1, shared=True):
        manifest = read_json(Path(cfg["state"]) / "manifest.json", {})
        changed = signature(scan(cfg["incoming"], content=False)) != manifest.get("incoming_signature")
    status_write(cfg, phase=("offline" if not usb["module_loaded"] else "waiting-for-eject" if changed else "ready"), pending=changed, usb=usb)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["boot", "auto", "refresh", "present", "stop", "status"])
    parser.add_argument("--host-ejected", action="store_true",
                        help="Explicitly assert the host has unmounted/flushed and stopped using the USB disk")
    args = parser.parse_args()
    cfg = load_config()
    if args.action == "status":
        print(json.dumps(read_json(Path(cfg["state"]) / "status.json", {}), indent=2))
        return 0
    if os.geteuid() != 0:
        parser.error("Run this command through sudo")
    try:
        with lock(Path(cfg["state"]) / "control.lock", timeout=1 if args.action == "auto" else 600):
            if args.action == "auto":
                poll(cfg)
            elif args.action == "boot":
                if (Path(cfg["state"]) / "recovery-required.json").exists():
                    raise SafetyError("Previous sync needs recovery; run mobileusb-refresh --host-ejected after inspection")
                sync_once(cfg, boot=True)
            elif args.action == "refresh":
                sync_once(cfg, acknowledged=args.host_ejected)
            elif args.action == "present":
                present(cfg)
            elif args.action == "stop":
                with lock(Path(cfg["state"]) / "data.lock", timeout=600):
                    disconnect(cfg)
                    status_write(cfg, phase="offline", usb=usb_state(cfg))
        return 0
    except Exception as e:
        text = str(e)
        if args.action == "auto" and "Storage is busy" in text:
            return 0
        status_write(cfg, phase="error", error=text)
        print(text, file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
