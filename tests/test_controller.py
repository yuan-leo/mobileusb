import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import controller
from common import SafetyError

class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.cfg={k:str(self.root/k) for k in ['state','requests','incoming','mount','image']}
        for k in ['state','requests','incoming','mount']: Path(self.cfg[k]).mkdir()
        Path(self.cfg['image']).write_bytes(b'not a real image')
        self.cfg.update(uid=os.getuid(),gid=os.getgid())
    def tearDown(self): self.tmp.cleanup()
    def test_refresh_refuses_active_writable_host(self):
        with patch.object(controller,'assert_our_image'),patch.object(controller,'usb_state',return_value={'module_loaded':True,'host_ejected':False}),patch.object(controller,'disconnect') as dis:
            with self.assertRaises(SafetyError): controller.sync_once(self.cfg)
            dis.assert_not_called()
    def test_auto_never_syncs_just_because_network_changed(self):
        with patch.object(controller,'usb_state',return_value={'module_loaded':True,'host_ejected':False}),patch.object(controller,'sync_once') as sync:
            controller.poll(self.cfg); sync.assert_not_called()
    def test_eject_triggers_sync(self):
        with patch.object(controller,'usb_state',return_value={'module_loaded':True,'host_ejected':True}),patch.object(controller,'sync_once') as sync:
            controller.poll(self.cfg); sync.assert_called_once_with(self.cfg,acknowledged=False)
    def test_explicit_handoff_triggers_sync(self):
        (Path(self.cfg['requests'])/'refresh.json').write_text(json.dumps({'host_ejected':True,'time':__import__('time').time(),'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}))
        with patch.object(controller,'usb_state',return_value={'module_loaded':True,'host_ejected':False}),patch.object(controller,'sync_once') as sync:
            controller.poll(self.cfg); sync.assert_called_once_with(self.cfg,acknowledged=True)
    def test_control_disconnect_requires_target_safe_ack(self):
        request=Path(self.cfg['requests'])/'control.json'
        request.write_text(json.dumps({'action':'disconnect','time':__import__('time').time(),
                                       'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}))
        with patch.object(controller,'disconnect') as dis:
            controller.poll(self.cfg)
            dis.assert_not_called()
        self.assertFalse(request.exists())
        self.assertIn('acknowledgement',json.loads((Path(self.cfg['state'])/'status.json').read_text())['error'])

    def test_control_disconnect_executes_once(self):
        request=Path(self.cfg['requests'])/'control.json'
        request.write_text(json.dumps({'action':'disconnect','target_safe':True,'time':__import__('time').time(),
                                       'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}))
        with patch.object(controller,'disconnect') as dis,patch.object(controller,'usb_state',return_value={'module_loaded':False,'udc':{},'udc_details':{},'lun_files':{},'host_ejected':False}):
            controller.poll(self.cfg)
            dis.assert_called_once_with(self.cfg)
        self.assertFalse(request.exists())
        self.assertEqual(json.loads((Path(self.cfg['state'])/'status.json').read_text())['phase'],'offline')

    def test_control_present_executes_without_sync(self):
        request=Path(self.cfg['requests'])/'control.json'
        request.write_text(json.dumps({'action':'present','time':__import__('time').time(),
                                       'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}))
        with patch.object(controller,'present') as present,patch.object(controller,'sync_once') as sync:
            controller.poll(self.cfg)
            present.assert_called_once_with(self.cfg)
            sync.assert_not_called()
        self.assertFalse(request.exists())

    def test_recovery_does_not_loop_automatically(self):
        (Path(self.cfg['state'])/'recovery-required.json').write_text('{}')
        with patch.object(controller,'sync_once') as sync: controller.poll(self.cfg); sync.assert_not_called()
    def test_unload_failure_propagates(self):
        with patch.object(controller,'assert_our_image'),patch.object(controller,'module_loaded',return_value=True),patch.object(controller,'run',side_effect=SafetyError('busy')):
            with self.assertRaises(SafetyError): controller.disconnect(self.cfg)
    def test_present_refuses_existing_local_mount(self):
        with patch.object(controller,'assert_our_image'),patch.object(controller,'ensure_not_local',side_effect=SafetyError('mounted')),patch.object(controller,'run') as run:
            with self.assertRaises(SafetyError): controller.present(self.cfg)
            run.assert_not_called()
    def test_present_refuses_recovery_marker(self):
        (Path(self.cfg['state'])/'recovery-required.json').write_text('{}')
        with patch.object(controller,'assert_our_image'),patch.object(controller,'ensure_not_local'),patch.object(controller,'run') as run:
            with self.assertRaises(SafetyError): controller.present(self.cfg)
            run.assert_not_called()
    def test_wrong_filesystem_not_formatted(self):
        cp=subprocess.CompletedProcess([],0,'ext4\n')
        with patch.object(controller,'assert_our_image'),patch.object(controller,'usb_state',return_value={'module_loaded':False,'host_ejected':False}),patch.object(controller,'disconnect'),patch.object(controller,'ensure_not_local'),patch.object(controller,'run',return_value=cp) as run:
            with self.assertRaises(SafetyError): controller.sync_once(self.cfg)
            self.assertEqual(run.call_count,1)
    def test_corrupt_filesystem_not_mounted(self):
        returns=[subprocess.CompletedProcess([],0,'vfat\n'),subprocess.CompletedProcess([],1,'dirty FAT')]
        with patch.object(controller,'assert_our_image'),patch.object(controller,'usb_state',return_value={'module_loaded':False,'host_ejected':False}),patch.object(controller,'disconnect'),patch.object(controller,'ensure_not_local'),patch.object(controller,'run',side_effect=returns) as run:
            with self.assertRaises(SafetyError): controller.sync_once(self.cfg)
            self.assertEqual(run.call_count,2)
        self.assertTrue((Path(self.cfg['state'])/'recovery-required.json').exists())

if __name__=='__main__': unittest.main()
