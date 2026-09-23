# Deployment record and snapshot scope

This repository packages the MobileUSB v2 source archive and the unchanged
self-contained upgrade installer supplied in the project conversation.
Application, installer, and test source files were not changed during repository
preparation. This is not a live export of the Pi and not a backup of its files.

## Last reported installation: September 23, 2026

- Hardware: original Raspberry Pi Zero W, Raspberry Pi OS Lite 32-bit.
- Hostname: `mobileusb`; login account: `leo`.
- Web file manager: `http://mobileusb.local:8080`.
- USB implementation: `g_mass_storage`, not a ConfigFS-created gadget.
- Existing unpartitioned FAT32 image: `/srv/mobileusb/usb.img`, reported as 24 GiB.
- Incoming tree: `/srv/mobileusb/incoming`.
- System units: `mobileusb.service`, `mobileusb-refresh.service`,
  `mobileusb-refresh.timer`, and `wireless-upload.service`.
- The old immediate-refresh path watcher was replaced with an ejection-aware
  timer. Writable USB ownership must be handed off before synchronization.

The user's installation log reports all 61 bundled tests passed. Initial service
startup then stopped because of an inconsistent FAT32 free-cluster summary.
The user reported successful completion of the image-backup, interactive repair,
read-only verification, recovery-marker archival, and service-restart steps.
No device content or recovery archive is included here. Subsequent changes made
on the live device have not been fetched or independently compared.

## Verification during repository preparation

- All 19 files in the original source archive exactly matched the installer's
  embedded payload before repository-only documentation was added.
- The application and installer code is unchanged.
- Local test rerun: 44 tests passed and 17 web tests were skipped because
  Flask/Werkzeug is not installed in this preparation environment.
- `test-results.log` is the historical package-build log, not a live-device log.
- No USB hardware, FAT mount, reboot, or power-loss test was performed here.

The README describes installation into an existing MobileUSB setup; this is not
an unattended blank-SD-card provisioning image. Do not rerun the installer merely
to publish the repository or inspect the currently working Pi.

## Repository contents and privacy

Only software, templates/assets, tests, the self-contained installer, and project
documentation are included. The USB image, incoming files, uploaded photos,
Wi-Fi credentials, SSH keys, generated web credentials, and runtime state are not
included. Test authentication values are test fixtures, not deployed credentials.
No new software license was selected during packaging.
