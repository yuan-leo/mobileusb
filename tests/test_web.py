import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
os.environ['MOBILEUSB_TESTING']='1'
try:
    from web import create_app
    from werkzeug.security import generate_password_hash
    AVAILABLE=True
except ModuleNotFoundError:
    AVAILABLE=False

@unittest.skipUnless(AVAILABLE, 'Flask/Werkzeug not installed in this test environment')
class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.cfg={k:str(self.root/k) for k in ['incoming','tmp','trash','state','requests','auth']}
        for k in ['incoming','tmp','trash','state','requests']: Path(self.cfg[k]).mkdir()
        Path(self.cfg['auth']).write_text(json.dumps({'username':'leo','password_hash':generate_password_hash('testing-only',method='pbkdf2:sha256:1000'),'secret':'testing-secret'}))
        self.app=create_app(self.cfg); self.app.testing=True; self.client=self.app.test_client()
        self.net=Path(self.cfg['incoming'])
        self.client.get('/login')
        with self.client.session_transaction() as sess: self.csrf=sess['csrf']
        self.client.post('/login',data={'username':'leo','password':'testing-only','csrf':self.csrf})
        with self.client.session_transaction() as sess: self.csrf=sess['csrf']
    def tearDown(self): self.tmp.cleanup()
    def post(self,path,data=None,**kw):
        data=dict(data or {},csrf=self.csrf); return self.client.post(path,data=data,**kw)
    def test_listing_renders(self): self.assertEqual(self.client.get('/').status_code,200)
    def test_login_required(self):
        self.post('/logout'); self.assertEqual(self.client.get('/').status_code,302)
    def test_csrf_required(self): self.assertEqual(self.client.post('/mkdir',data={'name':'unsafe'}).status_code,400)
    def test_folder_creation(self):
        self.assertEqual(self.post('/mkdir',{'name':'photos'}).status_code,302); self.assertTrue((self.net/'photos').is_dir())
    def test_upload(self):
        r=self.post('/upload',{'files':(io.BytesIO(b'abc'),'a.txt')},content_type='multipart/form-data'); self.assertEqual(r.status_code,302); self.assertEqual((self.net/'a.txt').read_bytes(),b'abc')
    def test_upload_collision_preserves_original(self):
        (self.net/'a.txt').write_bytes(b'old'); self.post('/upload',{'files':(io.BytesIO(b'new'),'a.txt')},content_type='multipart/form-data'); self.assertEqual((self.net/'a.txt').read_bytes(),b'old'); self.assertEqual((self.net/'a_1.txt').read_bytes(),b'new')
    def test_replace_to_trash(self):
        (self.net/'a.txt').write_bytes(b'old'); self.post('/upload',{'replace':'yes','files':(io.BytesIO(b'new'),'a.txt')},content_type='multipart/form-data'); self.assertEqual((self.net/'a.txt').read_bytes(),b'new'); self.assertTrue(list(Path(self.cfg['trash']).glob('*/data')))
    def test_download(self):
        (self.net/'a.txt').write_bytes(b'abc'); r=self.client.get('/download?path=a.txt'); self.assertEqual(r.data,b'abc'); self.assertIn('attachment',r.headers['Content-Disposition']); r.close()
    def test_rename(self):
        (self.net/'a.txt').write_bytes(b'abc'); self.post('/action',{'path':'a.txt','kind':'rename','name':'b.txt'}); self.assertTrue((self.net/'b.txt').exists())
    def test_delete_restore(self):
        (self.net/'a.txt').write_bytes(b'abc'); self.post('/action',{'path':'a.txt','kind':'delete'}); self.assertFalse((self.net/'a.txt').exists()); ident=next(Path(self.cfg['trash']).iterdir()).name; self.post('/trash',{'id':ident,'operation':'restore'}); self.assertEqual((self.net/'a.txt').read_bytes(),b'abc')
    def test_text_edit(self):
        from common import hash_file
        (self.net/'a.txt').write_bytes(b'abc'); self.post('/action',{'path':'a.txt','kind':'edit','digest':hash_file(self.net/'a.txt'),'content':'new'}); self.assertEqual((self.net/'a.txt').read_bytes(),b'new')
    def test_text_concurrent_edit_rejected(self):
        (self.net/'a.txt').write_bytes(b'abc'); r=self.post('/action',{'path':'a.txt','kind':'edit','digest':'wrong','content':'new'}); self.assertEqual(r.status_code,409); self.assertEqual((self.net/'a.txt').read_bytes(),b'abc')
    def test_traversal_blocked(self): self.assertEqual(self.client.get('/download?path=../auth').status_code,409)
    def test_symlink_blocked(self):
        (self.net/'bad').symlink_to(self.cfg['auth']); self.assertEqual(self.client.get('/download?path=bad').status_code,409)
    def test_sync_requires_handoff(self):
        self.assertEqual(self.post('/sync').status_code,409); self.assertFalse((Path(self.cfg['requests'])/'refresh.json').exists())
    def test_sync_only_enqueues(self):
        self.assertEqual(self.post('/sync',{'host_ejected':'yes'}).status_code,302); self.assertTrue((Path(self.cfg['requests'])/'refresh.json').exists())
    def test_status_describes_configured_target(self):
        live={'module_loaded':True,'host_ejected':False,
              'udc':{'20980000.usb':'configured'},
              'udc_details':{'20980000.usb':{'state':'configured','speed':'high-speed','function':'g_mass_storage'}},
              'lun_files':{'/sys/fake/lun0/file':'/srv/mobileusb/usb.img'}}
        with patch('web.usb_state',return_value=live):
            data=self.client.get('/status').get_json()
        self.assertEqual(data['target_view']['code'],'configured')
        self.assertIn('should see MobileUSB',data['target_view']['message'])
        self.assertEqual(data['target_view']['speed'],'high-speed')

    def test_usb_disconnect_requires_acknowledgement(self):
        r=self.post('/usb-control',{'action':'disconnect'})
        self.assertEqual(r.status_code,409)
        self.assertFalse((Path(self.cfg['requests'])/'control.json').exists())

    def test_usb_disconnect_only_enqueues_guarded_request(self):
        r=self.post('/usb-control',{'action':'disconnect','target_safe':'yes','path':''})
        self.assertEqual(r.status_code,302)
        data=json.loads((Path(self.cfg['requests'])/'control.json').read_text())
        self.assertEqual(data['action'],'disconnect')
        self.assertIs(data['target_safe'],True)

    def test_usb_present_only_enqueues_request(self):
        r=self.post('/usb-control',{'action':'present','path':''})
        self.assertEqual(r.status_code,302)
        data=json.loads((Path(self.cfg['requests'])/'control.json').read_text())
        self.assertEqual(data['action'],'present')
        self.assertNotIn('target_safe',data)

    def test_index_has_drag_drop_and_usb_controls(self):
        data=self.client.get('/').get_data(as_text=True)
        self.assertIn('id="drop-zone"',data)
        self.assertIn('Force disconnect',data)
        self.assertIn('Force present',data)

    def test_other_templates(self):
        (self.net/'a.txt').write_bytes(b'abc')
        for route in ['/trash','/action?kind=edit&path=a.txt','/action?kind=delete&path=a.txt','/action?kind=rename&path=a.txt']:
            with self.subTest(route=route): self.assertEqual(self.client.get(route).status_code,200)

if __name__=='__main__': unittest.main()
