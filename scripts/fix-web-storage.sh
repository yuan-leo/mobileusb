#!/bin/bash
# Repair only the web service mount layout; never mount or detach the USB image.
set -Eeuo pipefail
[ "$(id -u)" -eq 0 ] || { echo 'Run with sudo on the Pi.' >&2; exit 1; }
[ -d /run/systemd/system ] || { echo 'A running systemd system is required.' >&2; exit 1; }
UNIT=wireless-upload.service
DIR=/etc/systemd/system/wireless-upload.service.d
FIX="$DIR/90-mobileusb-shared-storage.conf"
[ "$(systemctl show "$UNIT" -p LoadState --value)" = loaded ] || {
    echo 'The system-level wireless-upload.service was not found.' >&2; exit 1;
}
for p in /srv/mobileusb/incoming /srv/mobileusb/.upload-tmp /srv/mobileusb/.trash /srv/mobileusb/.requests; do
    [ -d "$p" ] && [ ! -L "$p" ] || { echo "Missing or unexpected directory: $p" >&2; exit 1; }
done
[ -f /srv/mobileusb/usb.img ] && [ ! -L /srv/mobileusb/usb.img ] || {
    echo 'Expected USB backing image is missing or a symbolic link.' >&2; exit 1;
}
/usr/bin/python3 - <<'CHECK'
import os
paths=['/srv/mobileusb/incoming','/srv/mobileusb/.upload-tmp','/srv/mobileusb/.trash']
if len({os.stat(p).st_dev for p in paths}) != 1:
    raise SystemExit('These directories are actually on different filesystems; this sandbox-only fix is not appropriate.')
CHECK
install -d -m 755 "$DIR"
[ ! -L "$FIX" ] || { echo 'Refusing to replace a symbolic-link override.' >&2; exit 1; }
if [ -f "$FIX" ]; then
    BACKUP=$(mktemp -d /var/backups/mobileusb-web-storage.XXXXXXXX)
    chmod 700 "$BACKUP"
    cp -a -- "$FIX" "$BACKUP/"
    echo "Previous override backed up to $BACKUP"
fi
TEMP=$(mktemp "$DIR/.storage-fix.XXXXXXXX")
trap 'rm -f -- "$TEMP"' EXIT
cat > "$TEMP" <<'EOF'
[Service]
ReadWritePaths=
ReadWritePaths=/srv/mobileusb /var/lib/mobileusb/data.lock
ReadOnlyPaths=/srv/mobileusb/usb.img
EOF
chmod 644 "$TEMP"
chown root:root "$TEMP"
mv -f -- "$TEMP" "$FIX"
systemctl daemon-reload
# This restarts the website only. Finish any other web transfers before running.
systemctl restart "$UNIT"
systemctl is-active --quiet "$UNIT"
systemctl show "$UNIT" -p ActiveState -p SubState -p ReadWritePaths -p ReadOnlyPaths -p ProtectSystem
echo 'Web storage fix installed. Retry upload, delete to Trash, and restore.'
echo 'No USB gadget restart, image write, format, repair, or reboot was performed.'
