#!/usr/bin/env python3
"""Linux integration regression: reproduce EXDEV, then test the fixed mount layout.

Run with sudo. All mounts are confined to a private mount namespace and newly
created temporary test files. No MobileUSB data or services are accessed.
"""
import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def command(*args):
    subprocess.run(args, check=True)


def bind(path, readonly=False):
    command('mount', '--bind', str(path), str(path))
    if readonly:
        command('mount', '-o', 'remount,bind,ro', str(path))


def run_checks():
    command('mount', '--make-rprivate', '/')
    with tempfile.TemporaryDirectory(prefix='mobileusb-mount-test-') as directory:
        base=Path(directory)
        folders={k:base/k for k in ('incoming','.upload-tmp','.trash','.requests')}
        for folder in folders.values():
            folder.mkdir()
        image=base/'usb.img'
        image.write_bytes(b'untouched fake image')
        incoming, stage, trash=(folders[k] for k in ('incoming','.upload-tmp','.trash'))
        mounts=[]
        try:
            # Negative control: the prior ReadWritePaths mounted siblings separately.
            for folder in (incoming,stage,trash):
                bind(folder); mounts.append(folder)
            source=stage/'probe'
            source.write_bytes(b'complete upload')
            try:
                os.replace(source,incoming/'probe')
            except OSError as error:
                assert error.errno==errno.EXDEV, error
            else:
                raise AssertionError('Old per-directory bind layout did not reproduce EXDEV')
            print('PASS: old sandbox layout reproduces EXDEV on the same underlying filesystem',flush=True)
            while mounts:
                command('umount',str(mounts.pop()))

            # Positive control: the common writable parent keeps sibling moves atomic.
            bind(base); mounts.append(base)
            bind(image,readonly=True); mounts.append(image)
            os.replace(source,incoming/'probe')
            assert (incoming/'probe').read_bytes()==b'complete upload'
            record=trash/'record'; record.mkdir()
            os.replace(incoming/'probe',record/'data')
            os.replace(record/'data',incoming/'probe')
            edited=stage/'edit'; edited.write_bytes(b'edited upload')
            os.replace(incoming/'probe',record/'data')
            os.replace(edited,incoming/'probe')
            assert (record/'data').read_bytes()==b'complete upload'
            assert (incoming/'probe').read_bytes()==b'edited upload'
            print('PASS: upload, Trash, restore and replacement use atomic moves with the fixed layout',flush=True)
            try:
                with image.open('wb') as stream:
                    stream.write(b'must not write')
            except OSError as error:
                assert error.errno in (errno.EROFS,errno.EACCES), error
            else:
                raise AssertionError('USB image was writable inside web sandbox')
            assert image.read_bytes()==b'untouched fake image'
            print('PASS: USB image remains read-only in the web mount namespace',flush=True)
        finally:
            for path in reversed(mounts):
                command('umount',str(path))


def main():
    if sys.platform!='linux' or os.geteuid()!=0:
        raise SystemExit('Run with sudo on Linux; namespace regression must not be skipped for release.')
    if sys.argv[1:]==['--inside-namespace']:
        run_checks()
        return
    if sys.argv[1:]:
        raise SystemExit('Usage: sudo python3 scripts/check_storage_namespace.py')
    root=Path(__file__).resolve().parents[1]
    for path in (root/'install.sh',root/'scripts/fix-web-storage.sh'):
        source=path.read_text()
        assert 'ReadWritePaths=/srv/mobileusb /var/lib/mobileusb/data.lock' in source, path
        assert 'ReadOnlyPaths=/srv/mobileusb/usb.img' in source, path
        assert 'ReadWritePaths=/srv/mobileusb/incoming ' not in source, path
    # A mapped root cannot traverse every runner-owned checkout. Feed this test
    # through stdin, after validating repository configuration outside the namespace.
    subprocess.run(['unshare','--user','--map-root-user','--mount','--fork',
                    sys.executable,'-','--inside-namespace'],
                   input=Path(__file__).read_text(), text=True, check=True, cwd='/tmp')


if __name__=='__main__':
    main()
