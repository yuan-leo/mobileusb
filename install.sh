#!/bin/bash
# MobileUSB v2 upgrade. Run on the Pi, never on the Windows host.
set -Eeuo pipefail
umask 022
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
USER_NAME=${SUDO_USER:-leo}
ACK=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --host-ejected) ACK=1; shift ;;
        --user) USER_NAME=${2:?Supply a Linux username}; shift 2 ;;
        -h|--help) printf '%s\n' 'Usage: sudo bash mobileusb-upgrade.sh --host-ejected [--user leo]' 'Back up irreplaceable USB files first. Eject/unmount the USB disk on the target; leave the cable connected.'; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done
[ "$(id -u)" -eq 0 ] || { echo 'Run through sudo on the Raspberry Pi.' >&2; exit 1; }
[ "$ACK" -eq 1 ] || { echo 'Eject/unmount the disk on the target first, then add --host-ejected. Do not unplug its power cable.' >&2; exit 1; }
[[ "$USER_NAME" =~ ^[a-z_][a-z0-9_-]*$ ]] && [ "$USER_NAME" != root ] || { echo 'Invalid non-root username.' >&2; exit 1; }
USER_ID=$(id -u "$USER_NAME")
GROUP_ID=$(id -g "$USER_NAME")
USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
[ "$USER_HOME" = "/home/$USER_NAME" ] || { echo 'This installer requires the usual /home/USERNAME layout.' >&2; exit 1; }
[ -f /srv/mobileusb/usb.img ] && [ ! -L /srv/mobileusb/usb.img ] || { echo 'Expected regular backing file /srv/mobileusb/usb.img is missing.' >&2; exit 1; }
[ -d /srv/mobileusb/incoming ] && [ ! -L /srv/mobileusb/incoming ] || { echo 'Expected incoming directory is missing or is a symbolic link.' >&2; exit 1; }
[ -d /run/systemd/system ] || { echo 'This must run on the Pi with systemd.' >&2; exit 1; }
command -v apt-get >/dev/null || { echo 'This installer targets Raspberry Pi OS / Debian.' >&2; exit 1; }

# Install distribution-maintained dependencies, not arbitrary downloaded binaries.
apt-get update
apt-get install -y --no-install-recommends python3-flask gunicorn dosfstools
/usr/bin/python3 -c 'import flask, werkzeug, gunicorn'
[ -x /usr/bin/gunicorn ] || { echo 'gunicorn executable missing.' >&2; exit 1; }

# All tests run against temporary directories and mocked hardware, before replacing anything.
MOBILEUSB_TESTING=1 PYTHONPATH="$HERE/src" /usr/bin/python3 - "$HERE/tests" <<'PY'
import sys, unittest
suite = unittest.defaultTestLoader.discover(sys.argv[1])
result = unittest.TextTestRunner(verbosity=2).run(suite)
if not result.wasSuccessful() or result.skipped:
    raise SystemExit('Pre-install tests did not all pass. Existing implementation was not replaced.')
PY

STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="/var/backups/mobileusb/$STAMP"
mkdir -p "$BACKUP/files"
chmod 700 /var/backups/mobileusb "$BACKUP"
export USER_NAME USER_ID GROUP_ID USER_HOME BACKUP

/usr/bin/python3 - <<'PY'
import json, os, pathlib, shutil, subprocess
B=pathlib.Path(os.environ['BACKUP']); user=os.environ['USER_NAME']; home=os.environ['USER_HOME']; uid=os.environ['USER_ID']
paths=['/opt/mobileusb','/etc/mobileusb','/etc/systemd/system/mobileusb.service',
       '/etc/systemd/system/mobileusb-refresh.service','/etc/systemd/system/mobileusb-refresh.timer',
       '/etc/systemd/system/mobileusb-refresh.path','/etc/systemd/system/wireless-upload.service',
       '/usr/local/sbin/mobileusb-present','/usr/local/sbin/mobileusb-disconnect',
       '/usr/local/sbin/mobileusb-refresh','/usr/local/sbin/mobileusb-status',
       '/usr/local/sbin/mobileusb-rollback',home+'/wireless-upload.py',
       home+'/.config/systemd/user/wireless-upload.service']
existing=[]
for name in paths:
    p=pathlib.Path(name)
    if p.exists() or p.is_symlink():
        existing.append(name); d=B/'files'/name.lstrip('/'); d.parent.mkdir(parents=True,exist_ok=True)
        subprocess.run(['cp','-a','--',str(p),str(d)],check=True)
def query(args):
    return subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True).stdout.strip()
units=['mobileusb.service','mobileusb-refresh.path','mobileusb-refresh.timer','wireless-upload.service']
services={n:{'enabled':query(['systemctl','is-enabled',n]),'active':query(['systemctl','is-active',n])} for n in units}
base=['runuser','-u',user,'--','env','XDG_RUNTIME_DIR=/run/user/'+uid,'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/'+uid+'/bus','systemctl','--user']
userunit={x:query(base+['is-'+x,'wireless-upload.service']) for x in ['enabled','active']}
(B/'backup.json').write_text(json.dumps({'paths':paths,'existing':existing,'services':services,'user_service':userunit,'user':user,'uid':uid},indent=2))
PY
cp "$HERE/restore.py" "$BACKUP/restore.py"
printf '%s\n' "$BACKUP" > /var/backups/mobileusb/latest
chmod 600 /var/backups/mobileusb/latest

failure() {
    echo >&2
    echo 'Upgrade stopped. Do not re-enable the old rsync --delete watcher until target-created data is protected.' >&2
    echo "Configuration backup: $BACKUP" >&2
    echo "After inspecting any sync/filesystem error, rollback is available with: sudo python3 $BACKUP/restore.py --host-ejected" >&2
    echo 'The installer does not format or resize usb.img. The backup is of program/configuration files, NOT the USB image.' >&2
}
trap failure ERR

# Stop producers first, then wait for any existing refresh before unbinding.
runuser -u "$USER_NAME" -- env XDG_RUNTIME_DIR="/run/user/$USER_ID" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$USER_ID/bus" systemctl --user disable --now wireless-upload.service 2>/dev/null || true
systemctl disable --now mobileusb-refresh.path 2>/dev/null || true
systemctl disable --now mobileusb-refresh.timer 2>/dev/null || true
for i in $(seq 1 600); do
    ACTIVE=$(systemctl is-active mobileusb-refresh.service 2>/dev/null || true)
    case "$ACTIVE" in active|activating|deactivating) sleep 1 ;; *) break ;; esac
done
case "$ACTIVE" in active|activating|deactivating) echo 'An existing refresh did not finish; stop here and inspect it.' >&2; exit 1 ;; esac
systemctl stop wireless-upload.service 2>/dev/null || true
systemctl stop mobileusb.service
if [ -d /sys/module/g_mass_storage ]; then modprobe -r g_mass_storage; fi
[ ! -d /sys/module/g_mass_storage ] || { echo 'USB module did not unload.' >&2; exit 1; }
if findmnt -rn --mountpoint /mnt/mobileusb >/dev/null; then echo 'Image is still mounted locally; inspect before proceeding.' >&2; exit 1; fi
[ -z "$(losetup -j /srv/mobileusb/usb.img)" ] || { echo 'Existing loop mapping found; inspect before proceeding.' >&2; exit 1; }

install -d -m 755 /opt/mobileusb /etc/mobileusb /var/lib/mobileusb /mnt/mobileusb
cp -a "$HERE/src/." /opt/mobileusb/
install -d -m 755 /opt/mobileusb/tests
cp -a "$HERE/tests/." /opt/mobileusb/tests/
cp "$HERE/README.md" /opt/mobileusb/README.md
chown -R root:root /opt/mobileusb
chmod -R go-w /opt/mobileusb
install -d -o "$USER_ID" -g "$GROUP_ID" -m 700 /srv/mobileusb/.upload-tmp /srv/mobileusb/.trash /srv/mobileusb/.requests
chown "$USER_ID:$GROUP_ID" /srv/mobileusb/incoming
# Existing trusted owner remains; imported files are chowned after successful copies.
touch /var/lib/mobileusb/data.lock /var/lib/mobileusb/control.lock
chown root:"$GROUP_ID" /var/lib/mobileusb/data.lock
chmod 660 /var/lib/mobileusb/data.lock
chmod 600 /var/lib/mobileusb/control.lock

/usr/bin/python3 - <<'PY'
import json, os, pathlib, secrets
from werkzeug.security import generate_password_hash
uid,gid=int(os.environ['USER_ID']),int(os.environ['GROUP_ID'])
cfg={'incoming':'/srv/mobileusb/incoming','image':'/srv/mobileusb/usb.img','mount':'/mnt/mobileusb',
     'tmp':'/srv/mobileusb/.upload-tmp','trash':'/srv/mobileusb/.trash','requests':'/srv/mobileusb/.requests',
     'state':'/var/lib/mobileusb','auth':'/etc/mobileusb/web-auth.json','uid':uid,'gid':gid}
if len({os.stat(cfg[k]).st_dev for k in ['incoming','tmp','trash']}) != 1:
    raise SystemExit('Incoming, temporary uploads and trash must be on the same filesystem.')
pathlib.Path('/etc/mobileusb/config.json').write_text(json.dumps(cfg,indent=2)+'\n')
authpath=pathlib.Path(cfg['auth'])
if not authpath.exists():
    password=secrets.token_urlsafe(18)
    auth={'username':os.environ['USER_NAME'],'password_hash':generate_password_hash(password,method='pbkdf2:sha256:600000'),'secret':secrets.token_hex(32)}
    authpath.write_text(json.dumps(auth,indent=2)+'\n')
    login=pathlib.Path('/root/mobileusb-web-login.txt')
    login.write_text('URL: http://mobileusb.local:8080\nUsername: '+auth['username']+'\nPassword: '+password+'\n')
    login.chmod(0o600)
os.chown(authpath,0,gid); authpath.chmod(0o640)
PY

cat > /usr/local/sbin/mobileusb-refresh <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /opt/mobileusb/controller.py refresh "$@"
EOF
cat > /usr/local/sbin/mobileusb-present <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /opt/mobileusb/controller.py present "$@"
EOF
cat > /usr/local/sbin/mobileusb-disconnect <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /opt/mobileusb/controller.py stop "$@"
EOF
cat > /usr/local/sbin/mobileusb-status <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /opt/mobileusb/controller.py status
EOF
cat > /usr/local/sbin/mobileusb-rollback <<'EOF'
#!/bin/sh
set -eu
[ "$(id -u)" -eq 0 ] || { echo 'Use sudo.' >&2; exit 1; }
B=$(cat /var/backups/mobileusb/latest)
case "$B" in /var/backups/mobileusb/*) ;; *) exit 1 ;; esac
exec /usr/bin/python3 "$B/restore.py" "$@"
EOF
chmod 755 /usr/local/sbin/mobileusb-present /usr/local/sbin/mobileusb-disconnect /usr/local/sbin/mobileusb-refresh /usr/local/sbin/mobileusb-status /usr/local/sbin/mobileusb-rollback
chown root:root /usr/local/sbin/mobileusb-present /usr/local/sbin/mobileusb-disconnect /usr/local/sbin/mobileusb-refresh /usr/local/sbin/mobileusb-status /usr/local/sbin/mobileusb-rollback

cat > "$USER_HOME/wireless-upload.py" <<'EOF'
#!/usr/bin/python3
"""Compatibility launcher; the backed-up previous Flask script is unchanged in the backup."""
import os
os.execv('/usr/bin/gunicorn', ['gunicorn', '--chdir', '/opt/mobileusb', '--workers', '1', '--threads', '2', '--timeout', '600', '--bind', '0.0.0.0:8080', '--access-logfile', '-', '--error-logfile', '-', 'web:app'])
EOF
chown root:root "$USER_HOME/wireless-upload.py"
chmod 644 "$USER_HOME/wireless-upload.py"
cat > /etc/systemd/system/mobileusb.service <<'EOF'
[Unit]
Description=MobileUSB safe boot reconciliation and mass-storage gadget
After=local-fs.target systemd-modules-load.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/mobileusb/controller.py boot
ExecStop=/usr/bin/python3 /opt/mobileusb/controller.py stop
RemainAfterExit=yes
TimeoutStartSec=infinity
TimeoutStopSec=10min
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/mobileusb-refresh.service <<'EOF'
[Unit]
Description=MobileUSB ejection-aware synchronization
After=mobileusb.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/mobileusb/controller.py auto
TimeoutStartSec=infinity
TimeoutStopSec=10min
EOF
cat > /etc/systemd/system/mobileusb-refresh.timer <<'EOF'
[Unit]
Description=Check for a safe MobileUSB handoff every ten seconds
[Timer]
OnBootSec=30s
OnUnitInactiveSec=10s
AccuracySec=1s
Unit=mobileusb-refresh.service
[Install]
WantedBy=timers.target
EOF
cat > /etc/systemd/system/wireless-upload.service <<EOF
[Unit]
Description=MobileUSB authenticated web file manager
After=network.target mobileusb.service
[Service]
Type=simple
User=$USER_NAME
Group=$GROUP_ID
WorkingDirectory=/opt/mobileusb
ExecStart=/usr/bin/python3 $USER_HOME/wireless-upload.py
Environment=TMPDIR=/srv/mobileusb/.upload-tmp
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=3
UMask=0022
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/srv/mobileusb/incoming /srv/mobileusb/.upload-tmp /srv/mobileusb/.trash /srv/mobileusb/.requests /var/lib/mobileusb/data.lock
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable mobileusb.service wireless-upload.service mobileusb-refresh.timer
systemctl reset-failed mobileusb.service mobileusb-refresh.service 2>/dev/null || true

# Initial reconciliation happens only after the user's explicit ejection confirmation.
# A dirty FAT check fails closed; the old destructive watcher is never auto-restored.
if ! systemctl start mobileusb.service; then
    systemctl start wireless-upload.service
    echo 'The web manager is installed, but the image needs inspection before it can be presented.' >&2
    journalctl -u mobileusb.service -n 40 --no-pager >&2
    failure
    exit 1
fi
systemctl start wireless-upload.service mobileusb-refresh.timer
/usr/bin/python3 - <<'PY'
import time, urllib.request
for n in range(20):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8080/login',timeout=5) as r:
            if r.status==200:
                print('HTTP login page: OK'); break
    except Exception:
        time.sleep(1)
else:
    raise SystemExit('Web listener did not respond. Inspect journalctl -u wireless-upload.service.')
PY
trap - ERR
printf '\n%s\n' 'MobileUSB upgrade installed. No reboot is required.' "Configuration backup: $BACKUP" 'Login credentials (different from SSH):'
if [ -f /root/mobileusb-web-login.txt ]; then cat /root/mobileusb-web-login.txt; else echo 'Existing web credentials retained.'; fi
printf '\n%s\n' 'Backups contain program/configuration files, not usb.img or a snapshot of incoming.' 'Only eject/unmount-triggered or explicitly acknowledged sync is enabled. The old automatic disconnect watcher is disabled.' 'Status: sudo mobileusb-status' 'Rollback after host ejection: sudo mobileusb-rollback --host-ejected'
