# MobileUSB v2: web file manager and conservative two-way reconciliation

> Repository snapshot: application code and the self-contained upgrade installer
> are preserved from the v2 package. See [deployment state](docs/DEPLOYMENT_STATE.md)
> for the user-reported successful installation and the limits of this snapshot.
> See [publishing instructions](docs/PUBLISH.md) for the prepared Git repository.

## Target installation

Designed for the existing Raspberry Pi Zero W, Raspberry Pi OS 32-bit setup:

- `/srv/mobileusb/usb.img`: existing unpartitioned FAT32 backing image (previously reported as 24 GiB).
- `/srv/mobileusb/incoming`: canonical file tree.
- `g_mass_storage`, `removable=1`, `ro=0`, `stall=0`, `nofua=0`.
- Browser address remains `http://mobileusb.local:8080`.

This package is an installer, not a claim that the live Pi has been changed. It does not format or resize the image, edit boot configuration, alter USB identity, or bypass endpoint policies.

## Install

1. Back up irreplaceable files from both the existing USB volume and incoming.
2. Copy `mobileusb-upgrade.sh` to `/home/leo/` using SCP or an SFTP client.
3. Eject/unmount the USB volume on the target computer. Leave the USB cable connected so the Pi stays powered. Stop any other direct SFTP/SMB writes to incoming during installation.
4. Over Wi-Fi SSH run:

```bash
sudo bash /home/leo/mobileusb-upgrade.sh --host-ejected --user leo
```

The installer gets Flask, Gunicorn, and FAT tools from the OS package repositories, runs the supplied tests in temporary directories, saves a timestamped program/configuration backup, replaces the old scripts/services, performs the initial checked reconciliation, and opens the new web manager. No reboot is required. Installation stops before changing the implementation if any bundled test fails or is skipped.

The web username and a new random password are printed at the end. They are separate from the SSH password. Retrieve them with:

```bash
sudo cat /root/mobileusb-web-login.txt
```

The backup is under `/var/backups/mobileusb/`. It includes program/configuration files, NOT a snapshot of `usb.img`, incoming, or file data. Package-manager changes are not rolled back.

## What the browser can do

Browse folders, upload multiple files (file picker or drag-and-drop), replace existing incoming files, download, create folders, rename, edit UTF-8 text files up to 1 MiB, delete to Trash, restore or permanently remove Trash items, view live target-facing USB state, and request a safe synchronization. File deletions and overwrites made in the browser retain the prior incoming copy in `/srv/mobileusb/.trash` until you purge it.

The USB status panel reports whether the target should currently see MobileUSB, plus the live UDC state, negotiated speed, gadget function, backing image, queued-change state, and last sync. The web controls can queue a force disconnect (simulated unplug) or force present (simulated replug). Force disconnect requires explicit acknowledgement that the target is idle and no longer using the drive. Force present reconnects the existing USB image and does not silently synchronize queued browser changes.

Normal uploads choose an unused `_1`, `_2`, etc. name instead of overwriting. Enable the explicit replace checkbox to replace a matching file. Uploads are completed in `/srv/mobileusb/.upload-tmp` before becoming visible in incoming. Incoming, temporary uploads, and Trash must be on the same local filesystem.

Each file must be less than 4 GiB for FAT32. A multipart request is limited to approximately 4 GiB; split larger batches. Unicode names are supported, but Windows/FAT-invalid names, case-colliding local names, symlinks, hardlinks, and special files are rejected rather than silently lost.

## Important change: safe writable-USB ownership

A normal USB mass-storage gadget is a block device, not a live shared folder. The host can buffer writes privately. Pi-side `sync`, an idle delay, a lock, or a stable image timestamp cannot force that host cache to flush.

Therefore this upgrade disables the old upload-triggered immediate disconnect watcher. It does NOT periodically yank an actively mounted writable USB disk.

- Web changes are queued immediately in incoming.
- The timer checks every ten seconds for a genuine SCSI media-eject event (an empty live LUN `file` attribute).
- When such an event is detected, it unloads the gadget, checks the FAT filesystem without repairing it, mounts the image exclusively, reconciles files, verifies the result, unmounts, and reconnects the gadget.
- When a host does not issue the recognizable eject command, eject/unmount on the host, then use the browser's acknowledged **Sync now / reconnect USB** action. The acknowledgement is a user assertion, not proof of cache flushing. Do not use it while the target is reading, copying, printing, or writing from this drive.
- No physical unplug is required. Wi-Fi/SSH stay independent of logical USB reconnection.
- Boot also reconciles before presenting USB, provided no conflicting gadget is already active and no previous recovery is pending.
- On an error, the controller never knowingly binds a locally mounted image. FAT errors are not automatically repaired. Inspect the reported error before proceeding.

The timer and lock are not a substitute for safe host ejection. Fully transparent real-time writable sharing requires cooperation from the target or a different storage protocol.

## File semantics

The sync manifest is committed only after a successful image unmount. Content hashes, rather than FAT modification timestamps, classify changes:

| Change since last successful sync | Result |
|---|---|
| New incoming file | Published to USB |
| Incoming-only edit | Replaces old USB copy, without a false duplicate |
| New USB-only file | Imported into incoming, then retained on both sides |
| USB edit to a path also in incoming | Incoming version keeps its name; USB version becomes a duplicate |
| Both sides edit the same path differently | Incoming keeps its name; USB version becomes a duplicate |
| Identical bytes on both sides | No duplicate |
| Web/incoming deletion; USB copy unchanged | Removed from USB at next sync; not resurrected |
| Web/incoming deletion plus USB edit | Original path remains deleted; changed USB bytes are recovered under a duplicate name |
| USB-only deletion | Does not delete incoming; incoming version returns on sync |

Example: `report.pdf` and `report.target-0123456789abcdef.pdf`.

Duplicate names incorporate a SHA-256 content fingerprint. If a same-named duplicate already has identical bytes, it is reused. A genuine hash-prefix/name collision gets a deterministic additional suffix. File-vs-directory/case conflicts can use a root-level `target-<path-hash>-...` fallback. A modified duplicate is given a new hash suffix instead of a growing chain of suffixes.

First sync has no historical baseline, so differing existing versions are conservatively preserved as conflicts. USB-only files are imported, not erased. Thereafter the manifest distinguishes intentional incoming deletions/renames from new USB files.

USB host metadata folders (`System Volume Information`, `$RECYCLE.BIN`, `.Spotlight-V100`, `.Trashes`, `.fseventsd`) are not imported or removed. This does not claim that every vendor-specific metadata filename is excluded.

## Why it does not recurse

There is one reconciliation engine, one controller lock, and a shared incoming-data lock for browser mutations. The previous filesystem-triggered path unit is disabled. The timer only syncs after a safe handoff or explicit acknowledgement, and hash-based reconciliation converges. Importing a target file into incoming does not start a second copy engine or generate an endless family of duplicates.

Direct SCP/SMB writers do not honor the browser lock. Stop them before syncing; use the web manager for coordinated mutations. The engine checks for external changes and aborts when detected, but cannot make arbitrary external writers transactional.

## Runtime components

- `/opt/mobileusb/web.py`: Flask application, served by one Gunicorn process with two threads, as the non-root `leo` account.
- `/home/leo/wireless-upload.py`: compatibility launcher.
- `/opt/mobileusb/controller.py`: root-only USB ownership/mount orchestration.
- `/opt/mobileusb/reconcile.py`: hash-based merge and publication.
- `/var/lib/mobileusb/manifest.json`: last successful sync baseline.
- `/var/lib/mobileusb/recovery-required.json`: present during an incomplete/failed sync; never delete blindly.
- `/var/lib/mobileusb/status.json`: browser/CLI status.
- `mobileusb.service`: checked boot reconciliation and USB presentation.
- `mobileusb-refresh.timer` -> `mobileusb-refresh.service`: ejection/request polling.
- `wireless-upload.service`: now a **system-level** unit with `User=leo`. The old user-level unit is disabled and backed up.
- The old `mobileusb-refresh.path` remains on disk but is disabled.

## Verify

```bash
systemctl status mobileusb.service mobileusb-refresh.timer wireless-upload.service --no-pager
```

```bash
sudo mobileusb-status
```

```bash
sudo journalctl -u mobileusb.service -u mobileusb-refresh.service -u wireless-upload.service -n 80 --no-pager
```

Manual safe refresh after an actual target ejection/unmount:

```bash
sudo mobileusb-refresh --host-ejected
```

Without `--host-ejected`, the refresh command requires the controller itself to observe ejection (or no loaded mass-storage module). It refuses to remove a still-owned writable disk.

## Acceptance test on the actual hardware

1. In the web manager, upload `network-test.txt` with `network original` as content. Eject/unmount the target and synchronize. Verify it appears on USB.
2. On the target, create `target-test.txt`, and change `network-test.txt` to `target edit`. Eject/unmount and synchronize.
3. Verify incoming/web retains the original `network-test.txt`, imports `target-test.txt`, and adds one hash-named duplicate containing `target edit`.
4. Repeat an eject/sync without edits. Verify no further duplicates appear.
5. Delete `target-test.txt` in the web manager, sync, and verify it stays absent on both sides. Restore it from Trash and sync to restore it.
6. Repeat after a graceful reboot, ejecting the USB volume before rebooting the Pi.

## Recovery and rollback

If the installer or sync reports a dirty/corrupt FAT image, do not automatically run a repair tool on the only copy. Keep the target disconnected logically, inspect status/logs, and preserve the image before considering repairs. Mount/unmount/module operations are hardware-dependent and require validation on this Pi.

Restore the previous programs/services, after ejecting the target:

```bash
sudo mobileusb-rollback --host-ejected
```

If the wrapper was not installed because setup failed early, the installer prints an equivalent command pointing directly to the backup's `restore.py`.

Rollback does not rewind file data or purge imported duplicates/Trash. The restored older `rsync --delete` design again treats incoming as a one-way mirror and may delete future USB-only files.

## Security and limitations

Use the plain HTTP URL only on a trusted LAN or through an SSH tunnel. Login and CSRF defenses do not encrypt HTTP traffic; do not expose port 8080 directly to the Internet. Credentials are stored as a salted password hash; a root-readable copy of the generated initial login is retained for recovery. The web process runs without root privileges, with systemd filesystem restrictions.

This is not a full crash-proof transactional filesystem. Sudden power loss can still damage FAT or the underlying microSD. Each sync hashes the file trees, so large collections increase USB offline time. Incoming plus USB image content plus Trash/temporary uploads consume microSD capacity; the displayed 24 GiB image size does not mean 24 GiB remains available for another full incoming copy.

## Validation record

At package creation, the 34 reconciliation tests and 10 mocked-controller tests passed locally, including randomized repeated convergence. Python and shell syntax were checked. This environment could not fetch Flask dependencies, so the 17 HTTP/web tests were not executed locally. The installer requires all 61 tests, including those 17 web tests, to pass on the Pi before replacing any code/services. A live USB controller, FAT loop mount, Android behavior, reboot, and power-loss recovery were not exercised here.

## Primary technical references

- Linux Mass Storage Gadget documentation: https://cdn.kernel.org/doc/html/latest/usb/mass-storage.html
- Linux implementation and live LUN attributes: https://github.com/torvalds/linux/blob/master/drivers/usb/gadget/function/f_mass_storage.c
- Flask production deployment guidance: https://flask.palletsprojects.com/en/stable/deploying/
- Flask Gunicorn deployment: https://flask.palletsprojects.com/en/stable/deploying/gunicorn/
- Flask security considerations: https://flask.palletsprojects.com/en/stable/web-security/
