#!/usr/bin/python3
"""Restore pre-upgrade programs and service enablement. Never restore/delete file data."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

if os.geteuid()!=0 or '--host-ejected' not in sys.argv:
    raise SystemExit('Run with sudo and --host-ejected only after ejecting/unmounting the USB disk on the target.')
B=Path(__file__).resolve().parent
meta=json.loads((B/'backup.json').read_text())
def run(*args,check=False):
    return subprocess.run(args,check=check)
run('systemctl','stop','mobileusb-refresh.timer')
# Do not kill a live copy in the middle: wait for it first.
import time
for _ in range(600):
    state=subprocess.run(['systemctl','is-active','mobileusb-refresh.service'],capture_output=True,text=True).stdout.strip()
    if state not in ('active','activating','deactivating'): break
    time.sleep(1)
else: raise SystemExit('A refresh is still running. Inspect it before rolling back.')
run('systemctl','stop','wireless-upload.service','mobileusb.service')
if Path('/sys/module/g_mass_storage').exists(): run('modprobe','-r','g_mass_storage',check=True)
if subprocess.run(['findmnt','-rn','--mountpoint','/mnt/mobileusb'],capture_output=True).returncode==0:
    raise SystemExit('Image is still mounted; inspect and unmount it before rollback.')
if subprocess.run(['losetup','-j','/srv/mobileusb/usb.img'],capture_output=True,text=True,check=True).stdout.strip():
    raise SystemExit('Image still has a loop mapping; inspect before rollback.')
run('systemctl','disable','mobileusb-refresh.timer','wireless-upload.service')
for name in meta['paths']:
    p=Path(name)
    if p.is_symlink() or p.is_file(): p.unlink()
    elif p.is_dir(): shutil.rmtree(p)
    if name in meta['existing']:
        src=B/'files'/name.lstrip('/'); p.parent.mkdir(parents=True,exist_ok=True)
        run('cp','-a','--',str(src),str(p),check=True)
run('systemctl','daemon-reload',check=True)
for name,s in meta['services'].items():
    if s['enabled'] in ('enabled','enabled-runtime'): run('systemctl','enable',name)
    elif s['enabled']=='disabled': run('systemctl','disable',name)
    if s['active']=='active': run('systemctl','start',name)
u=meta['user']; uid=meta['uid']
base=['runuser','-u',u,'--','env','XDG_RUNTIME_DIR=/run/user/'+uid,'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/'+uid+'/bus','systemctl','--user']
run(*base,'daemon-reload')
if meta['user_service']['enabled'] in ('enabled','enabled-runtime'): run(*base,'enable','wireless-upload.service')
if meta['user_service']['active']=='active': run(*base,'start','wireless-upload.service')
print('Previous programs/services restored. Current incoming, USB image, Trash and sync-state data were NOT reverted.')
print('The restored old rsync --delete behavior may discard future USB-only files; back up target data before using it.')
