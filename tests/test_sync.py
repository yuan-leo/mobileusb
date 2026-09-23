import os
from pathlib import Path
import random
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from common import FAT_MAX, SafetyError, hash_file, safe_path, scan
from reconcile import reconcile

class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.net = self.root / 'incoming'; self.net.mkdir()
        self.usb = self.root / 'target'; self.usb.mkdir()
        self.manifest = self.root / 'state.json'
        self.mock_sync = patch('reconcile.os.sync'); self.mock_sync.start()
    def tearDown(self):
        self.mock_sync.stop(); self.tmp.cleanup()
    def put(self, root, path, data):
        p = root / path; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data)
    def sync(self):
        result = reconcile(self.net, self.usb, self.manifest)
        os.replace(str(self.manifest) + '.candidate', self.manifest)
        return result
    def values(self, root):
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    def equal(self):
        self.assertEqual(self.values(self.net), self.values(self.usb))
    def test_empty(self):
        self.sync(); self.equal()
    def test_initial_network_file(self):
        self.put(self.net, 'a.txt', b'network'); self.sync(); self.equal()
    def test_initial_target_file(self):
        self.put(self.usb, 'a.txt', b'host'); r=self.sync(); self.assertEqual(len(r['imported']),1); self.equal()
    def test_initial_conflict(self):
        self.put(self.net,'a.txt',b'network'); self.put(self.usb,'a.txt',b'host'); r=self.sync()
        self.assertEqual((self.net/'a.txt').read_bytes(),b'network'); self.assertEqual(len(r['duplicates']),1); self.assertIn(b'host', self.values(self.net).values()); self.equal()
    def test_network_update_no_false_duplicate(self):
        self.put(self.net,'a.txt',b'old'); self.sync(); self.put(self.net,'a.txt',b'new'); r=self.sync()
        self.assertEqual(r['duplicates'],[]); self.assertEqual(self.values(self.net),{'a.txt':b'new'}); self.equal()
    def test_target_update_duplicate(self):
        self.put(self.net,'a.txt',b'old'); self.sync(); self.put(self.usb,'a.txt',b'new'); r=self.sync()
        self.assertEqual((self.net/'a.txt').read_bytes(),b'old'); self.assertEqual(len(r['duplicates']),1); self.equal()
    def test_both_update(self):
        self.put(self.net,'a.txt',b'old'); self.sync(); self.put(self.net,'a.txt',b'web'); self.put(self.usb,'a.txt',b'host'); self.sync()
        self.assertEqual((self.net/'a.txt').read_bytes(),b'web'); self.assertIn(b'host',self.values(self.net).values()); self.equal()
    def test_same_simultaneous_update(self):
        self.put(self.net,'a.txt',b'old'); self.sync(); self.put(self.net,'a.txt',b'new'); self.put(self.usb,'a.txt',b'new'); self.sync()
        self.assertEqual(len(self.values(self.net)),1); self.equal()
    def test_web_delete_not_resurrected(self):
        self.put(self.net,'a.txt',b'old'); self.sync(); (self.net/'a.txt').unlink(); self.sync(); self.assertEqual(self.values(self.net),{}); self.equal()
    def test_host_delete_not_propagated(self):
        self.put(self.net,'a.txt',b'old'); self.sync(); (self.usb/'a.txt').unlink(); self.sync(); self.assertTrue((self.usb/'a.txt').exists())
    def test_delete_vs_host_edit(self):
        self.put(self.net,'a.txt',b'old'); self.sync(); (self.net/'a.txt').unlink(); self.put(self.usb,'a.txt',b'host'); self.sync()
        self.assertFalse((self.net/'a.txt').exists()); self.assertIn(b'host',self.values(self.net).values()); self.equal()
    def test_nested_import(self):
        self.put(self.usb,'photos/trip/a.txt',b'new'); self.sync(); self.equal(); self.assertTrue((self.net/'photos/trip/a.txt').exists())
    def test_empty_directory(self):
        (self.usb/'empty').mkdir(); self.sync(); self.assertTrue((self.net/'empty').is_dir())
    def test_deleted_empty_directory(self):
        (self.net/'empty').mkdir(); self.sync(); (self.net/'empty').rmdir(); self.sync(); self.assertFalse((self.usb/'empty').exists())
    def test_deleted_nested_directory(self):
        self.put(self.net,'folder/a.txt',b'old'); self.sync(); shutil.rmtree(self.net/'folder'); self.sync(); self.assertFalse((self.usb/'folder').exists())
    def test_host_new_file_in_deleted_folder_preserved_as_duplicate(self):
        self.put(self.net,'folder/a.txt',b'old'); self.sync(); shutil.rmtree(self.net/'folder'); self.put(self.usb,'folder/new.txt',b'new'); r=self.sync()
        self.assertEqual(len(r['duplicates']),1); self.assertFalse((self.net/'folder').exists()); self.assertIn(b'new',self.values(self.net).values()); self.equal()
    def test_case_only_rename(self):
        self.put(self.net,'a.txt',b'old'); self.sync(); (self.net/'a.txt').rename(self.net/'A.txt'); self.sync(); self.assertEqual(list(self.values(self.usb)),['A.txt'])
    def test_cross_side_case_collision(self):
        self.put(self.net,'A.txt',b'web'); self.put(self.usb,'a.txt',b'host'); self.sync(); self.equal(); self.assertIn(b'host',self.values(self.net).values())
    def test_local_case_collision_rejected(self):
        self.put(self.net,'A.txt',b'web'); self.put(self.net,'a.txt',b'other')
        with self.assertRaises(SafetyError): self.sync()
    def test_file_directory_collision(self):
        self.put(self.net,'folder/a.txt',b'web'); self.put(self.usb,'folder',b'host'); self.sync(); self.equal(); self.assertIn(b'host',self.values(self.net).values())
    def test_directory_file_collision(self):
        self.put(self.net,'folder',b'web'); self.put(self.usb,'folder/a.txt',b'host'); self.sync(); self.equal(); self.assertIn(b'host',self.values(self.net).values())
    def test_same_size_same_mtime_host_edit(self):
        self.put(self.net,'a.txt',b'AAAA'); self.sync(); old=(self.usb/'a.txt').stat(); self.put(self.usb,'a.txt',b'BBBB'); os.utime(self.usb/'a.txt',ns=(old.st_atime_ns,old.st_mtime_ns)); self.sync(); self.assertIn(b'BBBB',self.values(self.net).values())
    def test_repeat_no_recursion(self):
        self.put(self.net,'a.txt',b'web'); self.put(self.usb,'a.txt',b'host'); self.sync(); before=self.values(self.net)
        for _ in range(5):
            r=self.sync(); self.assertEqual(r['duplicates'],[]); self.assertEqual(r['published'],[]); self.assertEqual(self.values(self.net),before)
    def test_identical_conflict_reuses_name(self):
        self.put(self.net,'a.txt',b'web'); self.put(self.usb,'a.txt',b'host'); self.sync(); before=self.values(self.net)
        self.put(self.usb,'a.txt',b'host'); self.sync(); self.assertEqual(self.values(self.net),before)
    def test_duplicate_deleted_from_web_stays_deleted(self):
        self.put(self.net,'a.txt',b'web'); self.put(self.usb,'a.txt',b'host'); self.sync(); dup=next(p for p in self.net.iterdir() if '.target-' in p.name); dup.unlink(); self.sync(); self.assertEqual(self.values(self.net),{'a.txt':b'web'}); self.equal()
    def test_symlink_rejected(self):
        (self.net/'escape').symlink_to('/etc/passwd')
        with self.assertRaises(SafetyError): self.sync()
    def test_hardlink_rejected(self):
        self.put(self.net,'a',b'web'); os.link(self.net/'a',self.net/'b')
        with self.assertRaises(SafetyError): self.sync()
    def test_fat_size_limit(self):
        with (self.net/'large').open('wb') as f: f.truncate(FAT_MAX+1)
        with self.assertRaises(SafetyError): self.sync()
    def test_metadata_preserved_not_imported(self):
        self.put(self.usb,'System Volume Information/record',b'metadata'); self.sync(); self.assertFalse((self.net/'System Volume Information').exists()); self.assertTrue((self.usb/'System Volume Information/record').exists())
    def test_out_of_space_does_not_delete_target(self):
        self.put(self.usb,'a',b'host'); usage=shutil.disk_usage(self.net)
        with patch('reconcile.shutil.disk_usage',return_value=type(usage)(usage.total,usage.used,1)):
            with self.assertRaises(SafetyError): self.sync()
        self.assertEqual((self.usb/'a').read_bytes(),b'host'); self.assertFalse(self.manifest.exists())
    def test_baseline_not_committed_before_controller_unmount(self):
        self.put(self.net,'a',b'web'); reconcile(self.net,self.usb,self.manifest)
        self.assertFalse(self.manifest.exists()); self.assertTrue(Path(str(self.manifest)+'.candidate').exists())
    def test_retry_after_uncommitted_sync(self):
        self.put(self.net,'a',b'web'); self.put(self.usb,'a',b'host'); reconcile(self.net,self.usb,self.manifest); before=self.values(self.net); self.sync(); self.assertEqual(self.values(self.net),before)
    def test_safe_paths(self):
        for bad in ['../etc/passwd','/etc/passwd','a/../../b','a\\b','CON','hello:bad']:
            with self.subTest(path=bad), self.assertRaises(SafetyError): safe_path(self.net,bad,False)
    def test_randomized_convergence(self):
        r=random.Random(71)
        for cycle in range(40):
            for side in [self.net,self.usb]:
                for index in range(4):
                    path=f'folder/{index}.txt'; p=side/path
                    if r.random()<.65: self.put(side,path,f'{cycle}-{r.randrange(100000)}'.encode())
                    elif p.exists(): p.unlink()
            canonical=self.values(self.net)
            self.sync(); self.equal()
            for p,v in canonical.items(): self.assertEqual((self.net/p).read_bytes(),v)
            before=self.values(self.net); self.sync(); self.assertEqual(self.values(self.net),before)

if __name__=='__main__': unittest.main()
